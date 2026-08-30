"""Trusted local-PDF materialization for the Phase-8 shadow runtime.

The module accepts one explicitly named, Phase-3-recorded PDF and writes only
immutable content-addressed blobs below an explicit CAS root.  It never uses
the network, grants sharing authority, or dispatches data.  PDF inspection,
text extraction and rendering use the controlled system Poppler tools with a
bounded timeout.  All Python-owned descriptors use the shared FIX3 ownership
protocol.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
import fcntl
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import tempfile
import time
from typing import Callable, Iterator, Mapping, NoReturn, Sequence
import unicodedata

from factory_core.canonical import canonical_bytes, canonical_sha256
from factory_core.fd_ownership import OwnedDescriptor, run_cleanup
from factory_core.phase3_artifacts import (
    ArtifactAvailability,
    ArtifactLedgerOccurrence,
    ArtifactOccurrenceKind,
    Phase3ContractError,
    artifact_occurrence_from_dict,
    validate_artifact_occurrence,
)
from factory_core.reference_evidence import (
    REFERENCE_EVIDENCE_SCHEMA,
    ReferenceDocumentRecord,
    ReferenceEvidenceError,
    derive_reference_chunk_id,
    validate_canonical_reference_evidence,
    verify_reference_document_record,
)


REFERENCE_PACKAGE_SCHEMA = "reference-package-v2"
REFERENCE_PACKAGE_RECEIPT_SCHEMA = "reference-package-receipt-v2"
REFERENCE_TOOLCHAIN_SCHEMA = "reference-materializer-toolchain-v2"
REFERENCE_ALGORITHMS_SCHEMA = "reference-materializer-algorithms-v2"
CAS_BLOB_SCHEMA = "phase8-cas-blob-v1"

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_POPPLER_TOOLS = {
    "pdfinfo": Path("/usr/bin/pdfinfo"),
    "pdftoppm": Path("/usr/bin/pdftoppm"),
    "pdftotext": Path("/usr/bin/pdftotext"),
}
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class ReferenceBlockerCode(str, Enum):
    CONFIG_INVALID = "CONFIG_INVALID"
    DEADLINE_EXCEEDED = "DEADLINE_EXCEEDED"
    ROOT_INVALID = "ROOT_INVALID"
    INPUT_OUTSIDE_ROOT = "INPUT_OUTSIDE_ROOT"
    INPUT_NOT_PDF = "INPUT_NOT_PDF"
    INPUT_NOT_REGULAR = "INPUT_NOT_REGULAR"
    INPUT_SYMLINK = "INPUT_SYMLINK"
    INPUT_CHANGED = "INPUT_CHANGED"
    INPUT_TOO_LARGE = "INPUT_TOO_LARGE"
    OUTPUT_TOO_LARGE = "OUTPUT_TOO_LARGE"
    INPUT_IDENTITY_MISMATCH = "INPUT_IDENTITY_MISMATCH"
    CAS_INVALID = "CAS_INVALID"
    CAS_WRITE_FAILED = "CAS_WRITE_FAILED"
    CAS_MISSING = "CAS_MISSING"
    CAS_CORRUPT = "CAS_CORRUPT"
    SCRATCH_FAILED = "SCRATCH_FAILED"
    TOOL_UNAVAILABLE = "TOOL_UNAVAILABLE"
    TOOL_FAILED = "TOOL_FAILED"
    TOOL_TIMEOUT = "TOOL_TIMEOUT"
    PDF_MALFORMED = "PDF_MALFORMED"
    PDF_ENCRYPTED = "PDF_ENCRYPTED"
    PDF_ZERO_PAGES = "PDF_ZERO_PAGES"
    PDF_PAGE_LIMIT = "PDF_PAGE_LIMIT"
    TEXT_NOT_PRESENT = "TEXT_NOT_PRESENT"
    PNG_INVALID = "PNG_INVALID"
    METADATA_INVALID = "METADATA_INVALID"
    RECORD_INVALID = "RECORD_INVALID"
    PACKAGE_INVALID = "PACKAGE_INVALID"
    PACKAGE_CORRUPT = "PACKAGE_CORRUPT"


class ReferenceMaterializationError(RuntimeError):
    """Stable fail-closed public error."""

    def __init__(self, code: ReferenceBlockerCode | str, detail: str):
        self.code = code.value if isinstance(code, ReferenceBlockerCode) else str(code)
        self.detail = detail
        super().__init__(f"{self.code}: {detail}")


def _fail(code: ReferenceBlockerCode, detail: str) -> NoReturn:
    raise ReferenceMaterializationError(code, detail)


def _check_deadline(deadline: object | None, phase: str) -> None:
    """Use an optional caller-owned total budget without resetting it."""

    if deadline is None:
        return
    checker = getattr(deadline, "check", None)
    if not callable(checker):
        _fail(ReferenceBlockerCode.CONFIG_INVALID, "deadline must expose check()")
    try:
        try:
            checker(phase)
        except TypeError:
            checker()
    except ReferenceMaterializationError:
        raise
    except BaseException as exc:
        if getattr(exc, "code", None) in {
            "PHASE78_DEADLINE_EXCEEDED",
            "PHASE78_REQUEST_CANCELLED",
        }:
            raise
        raise ReferenceMaterializationError(
            ReferenceBlockerCode.DEADLINE_EXCEEDED,
            f"total deadline expired during {phase}",
        ) from exc


def _remaining_timeout(
    deadline: object | None, configured_seconds: int, phase: str
) -> float:
    _check_deadline(deadline, phase)
    if deadline is None:
        return float(configured_seconds)
    remaining = getattr(deadline, "remaining_seconds", None)
    if not callable(remaining):
        _fail(
            ReferenceBlockerCode.CONFIG_INVALID,
            "deadline must expose remaining_seconds()",
        )
    try:
        value = float(remaining())
    except (TypeError, ValueError, OverflowError) as exc:
        raise ReferenceMaterializationError(
            ReferenceBlockerCode.CONFIG_INVALID,
            "deadline returned an invalid remaining budget",
        ) from exc
    if value <= 0:
        _fail(
            ReferenceBlockerCode.DEADLINE_EXCEEDED,
            f"total deadline expired during {phase}",
        )
    return min(float(configured_seconds), value)


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _exact_mapping(value: object, keys: set[str], label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        _fail(ReferenceBlockerCode.PACKAGE_INVALID, f"{label} must be an object")
    if set(value) != keys:
        _fail(ReferenceBlockerCode.PACKAGE_INVALID, f"{label} fields differ")
    return value


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail(ReferenceBlockerCode.PACKAGE_INVALID, f"{label} is required")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        _fail(ReferenceBlockerCode.PACKAGE_INVALID, f"{label} is not UTF-8")
    return value.strip()


def _digest(value: object, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        _fail(ReferenceBlockerCode.PACKAGE_INVALID, f"{label} is not SHA-256")
    return value


@dataclass(frozen=True)
class CasBlobFact:
    schema_version: str
    blob_ref: str
    sha256: str
    byte_length: int

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "blob_ref": self.blob_ref,
            "sha256": self.sha256,
            "byte_length": self.byte_length,
        }


def _blob_fact(value: object, label: str) -> CasBlobFact:
    mapping = _exact_mapping(
        value,
        {"schema_version", "blob_ref", "sha256", "byte_length"},
        label,
    )
    if mapping["schema_version"] != CAS_BLOB_SCHEMA:
        _fail(ReferenceBlockerCode.PACKAGE_INVALID, f"{label} schema differs")
    digest = _digest(mapping["sha256"], f"{label}.sha256")
    if mapping["blob_ref"] != f"sha256:{digest}":
        _fail(ReferenceBlockerCode.PACKAGE_INVALID, f"{label} reference differs")
    length = mapping["byte_length"]
    if type(length) is not int or length < 0:
        _fail(ReferenceBlockerCode.PACKAGE_INVALID, f"{label} length is invalid")
    return CasBlobFact(CAS_BLOB_SCHEMA, f"sha256:{digest}", digest, length)


@contextmanager
def _owned_open(
    opener: Callable[[], int], *, owner: str, label: str
) -> Iterator[OwnedDescriptor]:
    lease = OwnedDescriptor.from_opener(opener, owner=owner, label=label)
    primary: BaseException | None = None
    try:
        yield lease
    except BaseException as error:
        primary = error
        raise
    finally:
        run_cleanup([(f"close {label}", lease.cleanup(owner))], primary=primary)


@contextmanager
def _cas_shard_lock(
    descriptor: int,
    *,
    exclusive: bool,
    deadline: object | None,
) -> Iterator[None]:
    """Hide the hard-link publication interval from concurrent readers."""

    operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    acquired = False
    primary: BaseException | None = None
    try:
        while True:
            _check_deadline(deadline, "CAS shard lock")
            try:
                fcntl.flock(descriptor, operation | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                time.sleep(0.001)
        yield
    except BaseException as error:
        primary = error
        raise
    finally:
        if acquired:
            run_cleanup(
                [
                    (
                        "unlock CAS shard",
                        lambda: fcntl.flock(descriptor, fcntl.LOCK_UN),
                    )
                ],
                primary=primary,
            )


def _directory_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )


def _file_flags(read_write: bool = False) -> int:
    mode = os.O_RDWR if read_write else os.O_RDONLY
    return mode | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)


def _same_inode(left: os.stat_result, right: os.stat_result) -> bool:
    return (int(left.st_dev), int(left.st_ino)) == (int(right.st_dev), int(right.st_ino))


def _require_absolute_directory(path: Path, label: str) -> Path:
    if not isinstance(path, Path):
        path = Path(path)
    if not path.is_absolute() or path == Path(path.anchor):
        _fail(ReferenceBlockerCode.ROOT_INVALID, f"{label} must be an absolute non-root path")
    try:
        current = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ReferenceMaterializationError(
            ReferenceBlockerCode.ROOT_INVALID, f"{label} is unavailable"
        ) from exc
    if stat.S_ISLNK(current.st_mode) or not stat.S_ISDIR(current.st_mode) or resolved != path:
        _fail(ReferenceBlockerCode.ROOT_INVALID, f"{label} must contain no symlink")
    return path


def _read_all(fd: int, limit: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        try:
            chunk = os.read(fd, min(1024 * 1024, limit + 1 - total))
        except OSError as exc:
            raise ReferenceMaterializationError(
                ReferenceBlockerCode.INPUT_CHANGED, "bounded read failed"
            ) from exc
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        total += len(chunk)
        if total > limit:
            _fail(ReferenceBlockerCode.INPUT_TOO_LARGE, "input exceeds configured limit")


def _relative_pdf(project_root: Path, pdf_path: Path) -> tuple[Path, str]:
    root = _require_absolute_directory(project_root, "project_root")
    candidate = Path(pdf_path)
    if not candidate.is_absolute():
        candidate = root / candidate
    candidate = Path(os.path.normpath(os.fspath(candidate)))
    try:
        relative = candidate.relative_to(root)
    except ValueError:
        _fail(ReferenceBlockerCode.INPUT_OUTSIDE_ROOT, "PDF is outside project root")
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        _fail(ReferenceBlockerCode.INPUT_OUTSIDE_ROOT, "PDF path is not canonical")
    if relative.suffix.lower() != ".pdf":
        _fail(ReferenceBlockerCode.INPUT_NOT_PDF, "input must use a .pdf suffix")
    return root, relative.as_posix()


def read_trusted_pdf(
    *,
    project_root: Path,
    pdf_path: Path,
    maximum_bytes: int,
    deadline: object | None = None,
) -> tuple[bytes, str]:
    """Securely read one explicit PDF below a real project root."""

    _check_deadline(deadline, "PDF read")
    if type(maximum_bytes) is not int or maximum_bytes < 1:
        _fail(ReferenceBlockerCode.CONFIG_INVALID, "maximum_bytes must be positive")
    root, relative = _relative_pdf(project_root, pdf_path)
    parts = Path(relative).parts
    leases: list[tuple[OwnedDescriptor, str]] = []
    primary: BaseException | None = None
    try:
        root_lease = OwnedDescriptor.from_opener(
            lambda: os.open(root, _directory_flags()),
            owner="reference-root",
            label="reference project root",
        )
        leases.append((root_lease, "reference-root"))
        parent_fd = root_lease.fileno("reference-root")
        root_stat = os.fstat(parent_fd)
        if not _same_inode(root_stat, root.lstat()):
            _fail(ReferenceBlockerCode.ROOT_INVALID, "project root identity changed")
        for ordinal, component in enumerate(parts[:-1]):
            owner = f"reference-dir-{ordinal}"
            before = os.stat(component, dir_fd=parent_fd, follow_symlinks=False)
            if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
                _fail(ReferenceBlockerCode.INPUT_SYMLINK, "PDF ancestry contains a symlink")
            lease = OwnedDescriptor.from_opener(
                lambda component=component, parent_fd=parent_fd: os.open(
                    component, _directory_flags(), dir_fd=parent_fd
                ),
                owner=owner,
                label=f"reference input directory {ordinal}",
            )
            leases.append((lease, owner))
            opened = os.fstat(lease.fileno(owner))
            if not _same_inode(before, opened):
                _fail(ReferenceBlockerCode.INPUT_CHANGED, "PDF ancestry changed")
            parent_fd = lease.fileno(owner)
        name = parts[-1]
        before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if stat.S_ISLNK(before.st_mode):
            _fail(ReferenceBlockerCode.INPUT_SYMLINK, "PDF is a symlink")
        if not stat.S_ISREG(before.st_mode) or int(before.st_nlink) != 1:
            _fail(ReferenceBlockerCode.INPUT_NOT_REGULAR, "PDF is not a single-link regular file")
        if int(before.st_size) > maximum_bytes:
            _fail(ReferenceBlockerCode.INPUT_TOO_LARGE, "PDF exceeds configured limit")
        file_lease = OwnedDescriptor.from_opener(
            lambda: os.open(name, _file_flags(), dir_fd=parent_fd),
            owner="reference-input",
            label="reference PDF",
        )
        leases.append((file_lease, "reference-input"))
        fd = file_lease.fileno("reference-input")
        opened = os.fstat(fd)
        if not _same_inode(before, opened):
            _fail(ReferenceBlockerCode.INPUT_CHANGED, "PDF changed before open")
        raw = _read_all(fd, maximum_bytes)
        _check_deadline(deadline, "PDF read")
        after = os.fstat(fd)
        named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        identity = lambda item: (
            int(item.st_dev), int(item.st_ino), int(item.st_mode), int(item.st_size),
            int(item.st_mtime_ns), int(item.st_ctime_ns),
        )
        if identity(opened) != identity(after) or identity(after) != identity(named):
            _fail(ReferenceBlockerCode.INPUT_CHANGED, "PDF changed during read")
        if len(raw) != int(opened.st_size):
            _fail(ReferenceBlockerCode.INPUT_CHANGED, "PDF byte count changed")
        if not raw.startswith(b"%PDF-"):
            _fail(ReferenceBlockerCode.INPUT_NOT_PDF, "input lacks a PDF header")
        return raw, relative
    except OSError as error:
        wrapped = ReferenceMaterializationError(
            ReferenceBlockerCode.INPUT_NOT_REGULAR,
            "PDF input is unavailable",
        )
        primary = wrapped
        raise wrapped from error
    except BaseException as error:
        primary = error
        raise
    finally:
        run_cleanup(
            [
                (f"close {lease.label}", lease.cleanup(owner))
                for lease, owner in reversed(leases)
            ],
            primary=primary,
        )


class ReferenceCas:
    """Explicit-root immutable SHA-256 content-addressed storage."""

    def __init__(self, root: Path):
        self.root = _require_absolute_directory(Path(root), "CAS root")

    @staticmethod
    def _parse_ref(blob_ref: object) -> str:
        if not isinstance(blob_ref, str) or not blob_ref.startswith("sha256:"):
            _fail(ReferenceBlockerCode.CAS_INVALID, "invalid CAS reference")
        digest = blob_ref[7:]
        if _SHA256.fullmatch(digest) is None:
            _fail(ReferenceBlockerCode.CAS_INVALID, "invalid CAS digest")
        return digest

    def _path(self, digest: str) -> Path:
        return self.root / "objects" / "sha256" / digest[:2] / digest

    def put(self, data: bytes, *, deadline: object | None = None) -> CasBlobFact:
        _check_deadline(deadline, "CAS put")
        if type(data) is not bytes:
            _fail(ReferenceBlockerCode.CAS_WRITE_FAILED, "CAS accepts exact bytes")
        digest = _sha(data)
        shard = self._path(digest).parent
        try:
            shard.mkdir(mode=0o700, parents=True, exist_ok=True)
            if shard.resolve(strict=True).is_relative_to(self.root) is False:
                _fail(ReferenceBlockerCode.CAS_INVALID, "CAS shard escaped root")
            if stat.S_ISLNK(shard.lstat().st_mode):
                _fail(ReferenceBlockerCode.CAS_INVALID, "CAS shard is a symlink")
        except OSError as exc:
            raise ReferenceMaterializationError(
                ReferenceBlockerCode.CAS_WRITE_FAILED, "cannot prepare CAS shard"
            ) from exc
        final = shard / digest
        with _owned_open(
            lambda: os.open(shard, _directory_flags()),
            owner="cas-shard",
            label="CAS shard",
        ) as shard_lease:
            shard_fd = shard_lease.fileno("cas-shard")
            try:
                existing = os.stat(digest, dir_fd=shard_fd, follow_symlinks=False)
            except FileNotFoundError:
                existing = None
            if existing is None:
                temporary = f".put-{digest}-{os.getpid()}-{secrets.token_hex(8)}"
                writer: OwnedDescriptor | None = None
                primary: BaseException | None = None
                try:
                    writer = OwnedDescriptor.from_opener(
                        lambda: os.open(
                            temporary,
                            os.O_WRONLY | os.O_CREAT | os.O_EXCL
                            | getattr(os, "O_CLOEXEC", 0)
                            | getattr(os, "O_NOFOLLOW", 0),
                            0o600,
                            dir_fd=shard_fd,
                        ),
                        owner="cas-writer",
                        label="CAS temporary",
                    )
                    fd = writer.fileno("cas-writer")
                    view = memoryview(data)
                    offset = 0
                    while offset < len(view):
                        written = os.write(fd, view[offset:])
                        if written <= 0:
                            _fail(ReferenceBlockerCode.CAS_WRITE_FAILED, "short CAS write")
                        offset += written
                    os.fsync(fd)
                    os.fchmod(fd, 0o400)
                    writer.close("cas-writer")
                    with _cas_shard_lock(
                        shard_fd, exclusive=True, deadline=deadline
                    ):
                        os.link(
                            temporary,
                            digest,
                            src_dir_fd=shard_fd,
                            dst_dir_fd=shard_fd,
                            follow_symlinks=False,
                        )
                        os.unlink(temporary, dir_fd=shard_fd)
                        os.fsync(shard_fd)
                except FileExistsError:
                    if writer is not None and not writer.closed:
                        writer.close("cas-writer")
                    try:
                        os.unlink(temporary, dir_fd=shard_fd)
                    except FileNotFoundError:
                        pass
                except BaseException as error:
                    primary = error
                    try:
                        os.unlink(temporary, dir_fd=shard_fd)
                    except FileNotFoundError:
                        pass
                    except BaseException as cleanup_error:
                        run_cleanup(
                            [("report CAS temporary cleanup", lambda: (_ for _ in ()).throw(cleanup_error))],
                            primary=error,
                        )
                    raise
                finally:
                    if writer is not None and not writer.closed:
                        run_cleanup(
                            [("close CAS writer", writer.cleanup("cas-writer"))],
                            primary=primary,
                        )
        fact = CasBlobFact(CAS_BLOB_SCHEMA, f"sha256:{digest}", digest, len(data))
        if self.get(
            fact.blob_ref,
            expected_length=fact.byte_length,
            deadline=deadline,
        ) != data:
            _fail(ReferenceBlockerCode.CAS_CORRUPT, "CAS reread differs")
        return fact

    def get(
        self,
        blob_ref: str,
        *,
        expected_length: int | None = None,
        deadline: object | None = None,
    ) -> bytes:
        _check_deadline(deadline, "CAS get")
        digest = self._parse_ref(blob_ref)
        path = self._path(digest)
        try:
            shard = path.parent
            with _owned_open(
                lambda: os.open(shard, _directory_flags()),
                owner="cas-reader-dir",
                label="CAS reader shard",
            ) as directory:
                parent_fd = directory.fileno("cas-reader-dir")
                with _cas_shard_lock(
                    parent_fd, exclusive=False, deadline=deadline
                ):
                    before = os.stat(
                        digest, dir_fd=parent_fd, follow_symlinks=False
                    )
                    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(
                        before.st_mode
                    ):
                        _fail(
                            ReferenceBlockerCode.CAS_CORRUPT,
                            "CAS blob is not regular",
                        )
                    if int(before.st_nlink) != 1 or before.st_mode & 0o222:
                        _fail(ReferenceBlockerCode.CAS_CORRUPT, "CAS blob is mutable")
                    with _owned_open(
                        lambda: os.open(digest, _file_flags(), dir_fd=parent_fd),
                        owner="cas-reader",
                        label="CAS blob",
                    ) as reader:
                        fd = reader.fileno("cas-reader")
                        opened = os.fstat(fd)
                        if not _same_inode(before, opened):
                            _fail(
                                ReferenceBlockerCode.CAS_CORRUPT,
                                "CAS blob changed before read",
                            )
                        data = _read_all(fd, max(int(opened.st_size), 1))
                        after = os.fstat(fd)
                        named = os.stat(
                            digest, dir_fd=parent_fd, follow_symlinks=False
                        )
                        if not _same_inode(opened, after) or not _same_inode(
                            after, named
                        ):
                            _fail(
                                ReferenceBlockerCode.CAS_CORRUPT,
                                "CAS blob changed during read",
                            )
        except FileNotFoundError as exc:
            raise ReferenceMaterializationError(
                ReferenceBlockerCode.CAS_MISSING, "CAS blob is missing"
            ) from exc
        except ReferenceMaterializationError:
            raise
        except OSError as exc:
            raise ReferenceMaterializationError(
                ReferenceBlockerCode.CAS_CORRUPT, "CAS read failed"
            ) from exc
        if expected_length is not None and len(data) != expected_length:
            _fail(ReferenceBlockerCode.CAS_CORRUPT, "CAS length differs")
        if _sha(data) != digest:
            _fail(ReferenceBlockerCode.CAS_CORRUPT, "CAS digest differs")
        _check_deadline(deadline, "CAS get")
        return data


@dataclass(frozen=True)
class ReferenceMaterializerConfig:
    maximum_pdf_bytes: int = 64 * 1024 * 1024
    maximum_component_bytes: int = 64 * 1024 * 1024
    maximum_total_output_bytes: int = 256 * 1024 * 1024
    maximum_pages: int = 512
    render_dpi: int = 144
    chunk_max_characters: int = 2000
    tool_timeout_seconds: int = 60

    def validate(self) -> "ReferenceMaterializerConfig":
        values = (
            self.maximum_pdf_bytes,
            self.maximum_component_bytes,
            self.maximum_total_output_bytes,
            self.maximum_pages,
            self.render_dpi,
            self.chunk_max_characters,
            self.tool_timeout_seconds,
        )
        if any(type(value) is not int or value < 1 for value in values):
            _fail(ReferenceBlockerCode.CONFIG_INVALID, "materializer limits must be positive integers")
        if (
            self.maximum_pdf_bytes > 1024 * 1024 * 1024
            or self.maximum_component_bytes > 1024 * 1024 * 1024
            or self.maximum_total_output_bytes > 4 * 1024 * 1024 * 1024
            or self.maximum_pages > 10_000
            or self.render_dpi > 600
            or self.chunk_max_characters > 1_000_000
            or self.tool_timeout_seconds > 600
        ):
            _fail(ReferenceBlockerCode.CONFIG_INVALID, "materializer limits exceed bounds")
        return self


@dataclass(frozen=True)
class MaterializedReferencePackage:
    package_blob: CasBlobFact
    receipt_blob: CasBlobFact
    package: Mapping[str, object]
    receipt: Mapping[str, object]
    record: ReferenceDocumentRecord

    def as_dict(self) -> dict[str, object]:
        return {
            "package_blob": self.package_blob.as_dict(),
            "receipt_blob": self.receipt_blob.as_dict(),
            "package": dict(self.package),
            "receipt": dict(self.receipt),
            "reference_document_record": self.record.as_dict(),
        }


def _canonical_json_bytes(value: Mapping[str, object]) -> bytes:
    try:
        return canonical_bytes(value)
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise ReferenceMaterializationError(
            ReferenceBlockerCode.PACKAGE_INVALID,
            "value is outside canonical JSON",
        ) from exc


def _decode_canonical_object(raw: bytes, label: str) -> dict[str, object]:
    duplicate = False

    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        nonlocal duplicate
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                duplicate = True
            result[key] = value
        return result

    try:
        value = json.loads(raw.decode("utf-8", errors="strict"), object_pairs_hook=pairs)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ReferenceMaterializationError(
            ReferenceBlockerCode.PACKAGE_CORRUPT, f"{label} is not canonical JSON"
        ) from exc
    if duplicate or type(value) is not dict or _canonical_json_bytes(value) != raw:
        _fail(ReferenceBlockerCode.PACKAGE_CORRUPT, f"{label} is not canonical JSON")
    return value


def _clean_text(raw: bytes) -> str:
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ReferenceMaterializationError(
            ReferenceBlockerCode.TOOL_FAILED, "pdftotext emitted invalid UTF-8"
        ) from exc
    text = unicodedata.normalize("NFKC", text.replace("\r\n", "\n").replace("\r", "\n"))
    lines = [line.rstrip() for line in text.replace("\f", "\n").split("\n")]
    normalized: list[str] = []
    blank = False
    for line in lines:
        if line:
            normalized.append(line)
            blank = False
        elif normalized and not blank:
            normalized.append("")
            blank = True
    result = "\n".join(normalized).strip()
    if not result:
        _fail(ReferenceBlockerCode.TEXT_NOT_PRESENT, "PDF page has no extractable text")
    return result + "\n"


def _png_dimensions(raw: bytes) -> tuple[int, int, str]:
    if not raw.startswith(_PNG_SIGNATURE) or len(raw) < 24 or raw[12:16] != b"IHDR":
        _fail(ReferenceBlockerCode.PNG_INVALID, "render output is not PNG")
    width = int.from_bytes(raw[16:20], "big")
    height = int.from_bytes(raw[20:24], "big")
    if width < 1 or height < 1:
        _fail(ReferenceBlockerCode.PNG_INVALID, "PNG dimensions are invalid")
    try:
        import PIL  # type: ignore[import-not-found]
        from PIL import Image  # type: ignore[import-not-found]

        with Image.open(BytesIO(raw)) as image:
            image.verify()
        with Image.open(BytesIO(raw)) as image:
            if image.format != "PNG" or image.size != (width, height):
                _fail(ReferenceBlockerCode.PNG_INVALID, "PNG decoder identity differs")
        implementation = f"Pillow/{getattr(PIL, '__version__', 'unknown')}"
    except ImportError:
        implementation = "stdlib-png-ihdr-v1"
    except ReferenceMaterializationError:
        raise
    except BaseException as exc:
        raise ReferenceMaterializationError(
            ReferenceBlockerCode.PNG_INVALID, "PNG decoder rejected render"
        ) from exc
    return width, height, implementation


def _tool_version(
    tool: Path, *, timeout: float, deadline: object | None = None
) -> str:
    try:
        result = subprocess.run(
            [os.fspath(tool), "-v"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout,
            env={**os.environ, "LC_ALL": "C", "LANG": "C"},
        )
    except subprocess.TimeoutExpired as exc:
        if deadline is not None:
            _check_deadline(deadline, f"{tool.name} version")
        raise ReferenceMaterializationError(
            ReferenceBlockerCode.TOOL_TIMEOUT,
            f"{tool.name} version probe timed out",
        ) from exc
    except OSError as exc:
        raise ReferenceMaterializationError(
            ReferenceBlockerCode.TOOL_UNAVAILABLE, f"{tool.name} is unavailable"
        ) from exc
    line = (result.stdout + result.stderr).decode("utf-8", errors="replace").splitlines()
    if result.returncode != 0 or not line:
        _fail(ReferenceBlockerCode.TOOL_UNAVAILABLE, f"{tool.name} version is unavailable")
    _check_deadline(deadline, f"{tool.name} version")
    return line[0].strip()


def _run_tool(
    args: Sequence[str],
    *,
    deadline: object | None,
    configured_timeout: int,
    label: str,
) -> subprocess.CompletedProcess[bytes]:
    timeout = _remaining_timeout(deadline, configured_timeout, label)
    try:
        result = subprocess.run(
            list(args),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout,
            env={**os.environ, "LC_ALL": "C", "LANG": "C"},
        )
    except subprocess.TimeoutExpired as exc:
        if deadline is not None:
            _check_deadline(deadline, label)
        raise ReferenceMaterializationError(
            ReferenceBlockerCode.TOOL_TIMEOUT, f"{label} timed out"
        ) from exc
    except OSError as exc:
        raise ReferenceMaterializationError(
            ReferenceBlockerCode.TOOL_UNAVAILABLE, f"{label} could not start"
        ) from exc
    _check_deadline(deadline, label)
    if result.returncode:
        diagnostic = result.stderr.decode("utf-8", errors="replace").lower()
        if "password" in diagnostic or "encrypted" in diagnostic:
            code = ReferenceBlockerCode.PDF_ENCRYPTED
        elif label == "pdfinfo":
            code = ReferenceBlockerCode.PDF_MALFORMED
        else:
            code = ReferenceBlockerCode.TOOL_FAILED
        raise ReferenceMaterializationError(code, f"{label} rejected the PDF")
    return result


def _write_scratch_pdf(directory: Path, raw: bytes) -> Path:
    path = directory / "input.pdf"
    lease: OwnedDescriptor | None = None
    primary: BaseException | None = None
    try:
        lease = OwnedDescriptor.from_opener(
            lambda: os.open(
                path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o400,
            ),
            owner="reference-scratch-writer",
            label="reference scratch PDF",
        )
        fd = lease.fileno("reference-scratch-writer")
        offset = 0
        while offset < len(raw):
            written = os.write(fd, raw[offset:])
            if written <= 0:
                _fail(ReferenceBlockerCode.SCRATCH_FAILED, "short scratch write")
            offset += written
        os.fsync(fd)
        lease.close("reference-scratch-writer")
        return path
    except BaseException as error:
        primary = error
        raise
    finally:
        if lease is not None and not lease.closed:
            run_cleanup(
                [("close reference scratch PDF", lease.cleanup("reference-scratch-writer"))],
                primary=primary,
            )


def _read_scratch_output(path: Path, *, maximum_bytes: int, label: str) -> bytes:
    try:
        before = path.lstat()
    except OSError as exc:
        raise ReferenceMaterializationError(
            ReferenceBlockerCode.TOOL_FAILED, f"{label} is unavailable"
        ) from exc
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISREG(before.st_mode)
        or int(before.st_nlink) != 1
    ):
        _fail(ReferenceBlockerCode.TOOL_FAILED, f"{label} is not a regular output")
    if int(before.st_size) > maximum_bytes:
        _fail(ReferenceBlockerCode.OUTPUT_TOO_LARGE, f"{label} exceeds its byte limit")
    try:
        with _owned_open(
            lambda: os.open(path, _file_flags()),
            owner="reference-output-reader",
            label=label,
        ) as reader:
            fd = reader.fileno("reference-output-reader")
            opened = os.fstat(fd)
            if not _same_inode(before, opened):
                _fail(ReferenceBlockerCode.TOOL_FAILED, f"{label} identity changed")
            try:
                raw = _read_all(fd, maximum_bytes)
            except ReferenceMaterializationError as exc:
                if exc.code == ReferenceBlockerCode.INPUT_TOO_LARGE.value:
                    _fail(
                        ReferenceBlockerCode.OUTPUT_TOO_LARGE,
                        f"{label} exceeds its byte limit",
                    )
                raise
            after = os.fstat(fd)
            named = path.lstat()
            if (
                not _same_inode(opened, after)
                or not _same_inode(after, named)
                or len(raw) != int(after.st_size)
            ):
                _fail(ReferenceBlockerCode.TOOL_FAILED, f"{label} changed during read")
            return raw
    except ReferenceMaterializationError:
        raise
    except OSError as exc:
        raise ReferenceMaterializationError(
            ReferenceBlockerCode.TOOL_FAILED, f"{label} read failed"
        ) from exc


def _parse_pdfinfo(raw: bytes, maximum_pages: int) -> tuple[int, bool]:
    values: dict[str, str] = {}
    for line in raw.decode("utf-8", errors="replace").splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            values[key.strip().lower()] = value.strip()
    try:
        pages = int(values["pages"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ReferenceMaterializationError(
            ReferenceBlockerCode.PDF_MALFORMED, "pdfinfo omitted a valid page count"
        ) from exc
    encrypted = values.get("encrypted", "no").lower().split()[0] not in {"no", "false"}
    if encrypted:
        _fail(ReferenceBlockerCode.PDF_ENCRYPTED, "encrypted PDF is unavailable")
    if pages < 1:
        _fail(ReferenceBlockerCode.PDF_ZERO_PAGES, "PDF contains no pages")
    if pages > maximum_pages:
        _fail(ReferenceBlockerCode.PDF_PAGE_LIMIT, "PDF exceeds configured page limit")
    return pages, encrypted


def _chunks(reference_id: str, pages: Sequence[str], maximum: int) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    ordinal = 0
    for page_number, text in enumerate(pages, start=1):
        remaining = text.strip()
        while remaining:
            if len(remaining) <= maximum:
                chunk = remaining
                remaining = ""
            else:
                boundary = remaining.rfind("\n", 0, maximum + 1)
                if boundary < maximum // 4:
                    boundary = remaining.rfind(" ", 0, maximum + 1)
                if boundary < 1:
                    boundary = maximum
                chunk = remaining[:boundary].rstrip()
                remaining = remaining[boundary:].lstrip()
            if not chunk:
                continue
            encoded = chunk.encode("utf-8")
            digest = _sha(encoded)
            result.append(
                {
                    "chunk_id": derive_reference_chunk_id(
                        reference_id=reference_id,
                        ordinal=ordinal,
                        page_start=page_number,
                        page_end=page_number,
                        text_sha256=digest,
                    ),
                    "ordinal": ordinal,
                    "page_start": page_number,
                    "page_end": page_number,
                    "text": chunk,
                    "text_sha256": digest,
                    "byte_length": len(encoded),
                }
            )
            ordinal += 1
    return result


def _materialized_occurrence(value: object, raw: bytes, relative: str) -> ArtifactLedgerOccurrence:
    try:
        occurrence = (
            validate_artifact_occurrence(value)
            if type(value) is ArtifactLedgerOccurrence
            else artifact_occurrence_from_dict(value)
        )
    except (Phase3ContractError, TypeError, ValueError) as exc:
        raise ReferenceMaterializationError(
            ReferenceBlockerCode.INPUT_IDENTITY_MISMATCH,
            "Phase-3 artifact occurrence does not revalidate",
        ) from exc
    record = occurrence.artifact_record
    if occurrence.kind is not ArtifactOccurrenceKind.RECORD or record is None:
        _fail(ReferenceBlockerCode.INPUT_IDENTITY_MISMATCH, "PDF is not a recorded artifact")
    if (
        record.availability is not ArtifactAvailability.RECORDED
        or record.normalized_path != relative
        or record.content_sha256 != _sha(raw)
        or record.byte_length != len(raw)
    ):
        _fail(
            ReferenceBlockerCode.INPUT_IDENTITY_MISMATCH,
            "PDF bytes/path differ from the Phase-3 occurrence",
        )
    return occurrence


def _metadata(value: object) -> dict[str, object]:
    expected = {"title", "authors", "published_year", "doi"}
    if not isinstance(value, Mapping) or set(value) != expected:
        _fail(ReferenceBlockerCode.METADATA_INVALID, "metadata fields differ")
    title = value["title"]
    authors = value["authors"]
    year = value["published_year"]
    doi = value["doi"]
    if not isinstance(title, str) or not title.strip():
        _fail(ReferenceBlockerCode.METADATA_INVALID, "metadata title is required")
    if (
        not isinstance(authors, (list, tuple))
        or not authors
        or any(not isinstance(item, str) or not item.strip() for item in authors)
    ):
        _fail(ReferenceBlockerCode.METADATA_INVALID, "metadata authors are required")
    normalized_authors = [item.strip() for item in authors]
    if len(set(normalized_authors)) != len(normalized_authors):
        _fail(ReferenceBlockerCode.METADATA_INVALID, "metadata authors must be unique")
    if type(year) is not int or not 1 <= year <= 9999:
        _fail(ReferenceBlockerCode.METADATA_INVALID, "metadata year is invalid")
    if doi is not None and (not isinstance(doi, str) or not doi.strip()):
        _fail(ReferenceBlockerCode.METADATA_INVALID, "metadata DOI is invalid")
    return {
        "title": title.strip(),
        "authors": normalized_authors,
        "published_year": year,
        "doi": None if doi is None else doi.strip(),
    }


def materialize_reference_pdf(
    *,
    reference_id: str,
    project_root: Path,
    pdf_path: Path,
    phase3_artifact_occurrence: object,
    bibliographic_metadata: Mapping[str, object],
    external_share_classification: str,
    cas_root: Path,
    scratch_root: Path,
    config: ReferenceMaterializerConfig | None = None,
    deadline: object | None = None,
) -> MaterializedReferencePackage:
    """Materialize one recorded local PDF into immutable, replayable CAS facts."""

    settings = (config or ReferenceMaterializerConfig()).validate()
    ref_id = _identifier(reference_id, "reference_id")
    classification = _identifier(external_share_classification, "classification")
    if classification not in {"public", "internal", "confidential", "restricted"}:
        _fail(ReferenceBlockerCode.METADATA_INVALID, "classification is unsupported")
    scratch = _require_absolute_directory(Path(scratch_root), "scratch_root")
    cas = ReferenceCas(Path(cas_root))
    _check_deadline(deadline, "materialization start")
    raw, relative = read_trusted_pdf(
        project_root=Path(project_root),
        pdf_path=Path(pdf_path),
        maximum_bytes=settings.maximum_pdf_bytes,
        deadline=deadline,
    )
    occurrence = _materialized_occurrence(phase3_artifact_occurrence, raw, relative)
    metadata = _metadata(bibliographic_metadata)
    raw_blob = cas.put(raw, deadline=deadline)

    for name, tool in _POPPLER_TOOLS.items():
        if not tool.is_absolute() or not tool.is_file() or not os.access(tool, os.X_OK):
            _fail(ReferenceBlockerCode.TOOL_UNAVAILABLE, f"{name} is unavailable")
    versions = {
        name: {
            "implementation": os.fspath(tool.resolve(strict=True)),
            "version": _tool_version(
                tool,
                timeout=_remaining_timeout(deadline, settings.tool_timeout_seconds, f"{name} version"),
                deadline=deadline,
            ),
        }
        for name, tool in sorted(_POPPLER_TOOLS.items())
    }

    page_evidence: list[dict[str, object]] = []
    page_texts: list[str] = []
    render_components: list[dict[str, object]] = []
    text_components: list[dict[str, object]] = []
    png_decoder = "unknown"
    total_output_bytes = 0
    try:
        with tempfile.TemporaryDirectory(prefix="phase8-materialize-", dir=scratch) as name:
            directory = Path(name)
            if directory.resolve(strict=True).parent != scratch:
                _fail(ReferenceBlockerCode.SCRATCH_FAILED, "scratch directory escaped root")
            input_pdf = _write_scratch_pdf(directory, raw)
            info = _run_tool(
                [os.fspath(_POPPLER_TOOLS["pdfinfo"]), os.fspath(input_pdf)],
                deadline=deadline,
                configured_timeout=settings.tool_timeout_seconds,
                label="pdfinfo",
            )
            page_count, _encrypted = _parse_pdfinfo(info.stdout, settings.maximum_pages)
            for page_number in range(1, page_count + 1):
                _check_deadline(deadline, f"page {page_number}")
                prefix = directory / f"page-{page_number:06d}"
                _run_tool(
                    [
                        os.fspath(_POPPLER_TOOLS["pdftoppm"]),
                        "-f", str(page_number), "-l", str(page_number),
                        "-singlefile", "-png", "-r", str(settings.render_dpi),
                        os.fspath(input_pdf), os.fspath(prefix),
                    ],
                    deadline=deadline,
                    configured_timeout=settings.tool_timeout_seconds,
                    label=f"pdftoppm page {page_number}",
                )
                png_path = prefix.with_suffix(".png")
                png = _read_scratch_output(
                    png_path,
                    maximum_bytes=settings.maximum_component_bytes,
                    label=f"render output page {page_number}",
                )
                total_output_bytes += len(png)
                if total_output_bytes > settings.maximum_total_output_bytes:
                    _fail(
                        ReferenceBlockerCode.OUTPUT_TOO_LARGE,
                        "materialized outputs exceed total byte limit",
                    )
                width, height, png_decoder = _png_dimensions(png)
                render_blob = cas.put(png, deadline=deadline)
                text_path = directory / f"page-{page_number:06d}.txt"
                _run_tool(
                    [
                        os.fspath(_POPPLER_TOOLS["pdftotext"]),
                        "-f", str(page_number), "-l", str(page_number),
                        "-enc", "UTF-8", "-nopgbrk",
                        os.fspath(input_pdf), os.fspath(text_path),
                    ],
                    deadline=deadline,
                    configured_timeout=settings.tool_timeout_seconds,
                    label=f"pdftotext page {page_number}",
                )
                text_output = _read_scratch_output(
                    text_path,
                    maximum_bytes=settings.maximum_component_bytes,
                    label=f"text output page {page_number}",
                )
                total_output_bytes += len(text_output)
                if total_output_bytes > settings.maximum_total_output_bytes:
                    _fail(
                        ReferenceBlockerCode.OUTPUT_TOO_LARGE,
                        "materialized outputs exceed total byte limit",
                    )
                text = _clean_text(text_output)
                text_raw = text.encode("utf-8")
                text_blob = cas.put(text_raw, deadline=deadline)
                page_texts.append(text)
                page_evidence.append(
                    {
                        "page_number": page_number,
                        "page_label": str(page_number),
                        "render": {
                            "media_type": "image/png",
                            "sha256": render_blob.sha256,
                            "byte_length": render_blob.byte_length,
                            "width_px": width,
                            "height_px": height,
                        },
                        "canonical_text": {
                            "text": text,
                            "sha256": text_blob.sha256,
                            "byte_length": text_blob.byte_length,
                        },
                    }
                )
                render_components.append(
                    {"page_number": page_number, "blob": render_blob.as_dict()}
                )
                text_components.append(
                    {"page_number": page_number, "blob": text_blob.as_dict()}
                )
    except ReferenceMaterializationError:
        raise
    except OSError as exc:
        raise ReferenceMaterializationError(
            ReferenceBlockerCode.SCRATCH_FAILED, "scratch operation failed"
        ) from exc

    chunk_evidence = _chunks(ref_id, page_texts, settings.chunk_max_characters)
    chunk_components: list[dict[str, object]] = []
    for chunk in chunk_evidence:
        blob = cas.put(str(chunk["text"]).encode("utf-8"), deadline=deadline)
        chunk_components.append({"chunk_id": chunk["chunk_id"], "blob": blob.as_dict()})
    metadata_components: list[dict[str, object]] = []
    provenance: dict[str, object] = {}
    for field in sorted(metadata):
        blob = cas.put(canonical_bytes(metadata[field]), deadline=deadline)
        metadata_components.append({"field": field, "blob": blob.as_dict()})
        provenance[field] = {
            "source_kind": "operator-declared-cas",
            "source_ref": blob.blob_ref,
            "value_sha256": canonical_sha256(metadata[field]),
        }

    evidence: dict[str, object] = {
        "schema_version": REFERENCE_EVIDENCE_SCHEMA,
        "reference_id": ref_id,
        "raw_pdf": {
            "blob_ref": raw_blob.blob_ref,
            "sha256": raw_blob.sha256,
            "byte_length": raw_blob.byte_length,
        },
        "pdf_inspection": {
            "status": "valid",
            "pdf_sha256": raw_blob.sha256,
            "page_count": len(page_evidence),
            "encrypted": False,
        },
        "pages": page_evidence,
        "chunks": chunk_evidence,
        "bibliographic_metadata": metadata,
        "metadata_provenance": provenance,
        "external_share_classification": classification,
    }
    try:
        record = validate_canonical_reference_evidence(evidence)
    except ReferenceEvidenceError as exc:
        raise ReferenceMaterializationError(
            ReferenceBlockerCode.RECORD_INVALID, "reference evidence did not revalidate"
        ) from exc
    record_blob = cas.put(_canonical_json_bytes(record.as_dict()), deadline=deadline)
    occurrence_wire = json.loads(canonical_bytes(occurrence.as_dict()).decode("utf-8"))
    occurrence_sha = canonical_sha256(occurrence_wire)
    options = {
        "maximum_pdf_bytes": settings.maximum_pdf_bytes,
        "maximum_component_bytes": settings.maximum_component_bytes,
        "maximum_total_output_bytes": settings.maximum_total_output_bytes,
        "maximum_pages": settings.maximum_pages,
        "render_dpi": settings.render_dpi,
        "chunk_max_characters": settings.chunk_max_characters,
        "tool_timeout_seconds": settings.tool_timeout_seconds,
    }
    toolchain_body: dict[str, object] = {
        "schema_version": REFERENCE_TOOLCHAIN_SCHEMA,
        "tools": versions,
        "png_decoder": png_decoder,
        "options": options,
    }
    toolchain = {**toolchain_body, "toolchain_sha256": canonical_sha256(toolchain_body)}
    algorithm_body: dict[str, object] = {
        "schema_version": REFERENCE_ALGORITHMS_SCHEMA,
        "text_normalization": "unicode-nfkc-lines-v1",
        "chunking": "bounded-page-local-v1",
        "rendering": "poppler-png-single-page-v1",
        "cas": "immutable-sha256-fsync-reread-v1",
    }
    algorithms = {**algorithm_body, "algorithms_sha256": canonical_sha256(algorithm_body)}
    components: dict[str, object] = {
        "raw_pdf": raw_blob.as_dict(),
        "record": record_blob.as_dict(),
        "page_renders": render_components,
        "page_texts": text_components,
        "chunks": chunk_components,
        "metadata_values": metadata_components,
    }
    components_sha = canonical_sha256(components)
    package_body: dict[str, object] = {
        "schema_version": REFERENCE_PACKAGE_SCHEMA,
        "reference_id": ref_id,
        "phase3_artifact_occurrence": occurrence_wire,
        "phase3_artifact_occurrence_sha256": occurrence_sha,
        "source": {
            "normalized_path": relative,
            "content_sha256": raw_blob.sha256,
            "byte_length": raw_blob.byte_length,
        },
        "reference_document_record": record.as_dict(),
        "reference_document_record_sha256": record.record_sha256,
        "toolchain": toolchain,
        "algorithms": algorithms,
        "components": components,
        "components_sha256": components_sha,
        "authoritative": False,
        "authority_transferred": False,
        "dispatch_performed": False,
    }
    package = {**package_body, "package_sha256": canonical_sha256(package_body)}
    package_blob = cas.put(_canonical_json_bytes(package), deadline=deadline)
    receipt_body: dict[str, object] = {
        "schema_version": REFERENCE_PACKAGE_RECEIPT_SCHEMA,
        "reference_id": ref_id,
        "package_sha256": package["package_sha256"],
        "package_blob": package_blob.as_dict(),
        "reference_document_record_sha256": record.record_sha256,
        "phase3_artifact_occurrence_sha256": occurrence_sha,
        "components_sha256": components_sha,
        "authoritative": False,
        "authority_transferred": False,
        "dispatch_performed": False,
    }
    receipt = {**receipt_body, "receipt_sha256": canonical_sha256(receipt_body)}
    receipt_blob = cas.put(_canonical_json_bytes(receipt), deadline=deadline)
    _check_deadline(deadline, "materialization complete")
    return MaterializedReferencePackage(package_blob, receipt_blob, package, receipt, record)


def _verify_false_safety(value: Mapping[str, object], label: str) -> None:
    for field in ("authoritative", "authority_transferred", "dispatch_performed"):
        if value.get(field) is not False:
            _fail(ReferenceBlockerCode.PACKAGE_INVALID, f"{label}.{field} must be false")


def load_reference_package(
    *,
    cas_root: Path,
    package_blob: Mapping[str, object] | CasBlobFact,
    receipt_blob: Mapping[str, object] | CasBlobFact,
    deadline: object | None = None,
) -> MaterializedReferencePackage:
    """Load and deeply verify only persisted CAS facts; never reopen the PDF."""

    cas = ReferenceCas(Path(cas_root))
    package_fact = package_blob if type(package_blob) is CasBlobFact else _blob_fact(package_blob, "package_blob")
    receipt_fact = receipt_blob if type(receipt_blob) is CasBlobFact else _blob_fact(receipt_blob, "receipt_blob")
    package_raw = cas.get(
        package_fact.blob_ref, expected_length=package_fact.byte_length, deadline=deadline
    )
    receipt_raw = cas.get(
        receipt_fact.blob_ref, expected_length=receipt_fact.byte_length, deadline=deadline
    )
    package = _decode_canonical_object(package_raw, "reference package")
    receipt = _decode_canonical_object(receipt_raw, "reference package receipt")
    if set(package) != {
        "schema_version", "reference_id", "phase3_artifact_occurrence",
        "phase3_artifact_occurrence_sha256", "source",
        "reference_document_record", "reference_document_record_sha256",
        "toolchain", "algorithms", "components", "components_sha256",
        "authoritative", "authority_transferred", "dispatch_performed",
        "package_sha256",
    }:
        _fail(ReferenceBlockerCode.PACKAGE_INVALID, "package fields differ")
    if set(receipt) != {
        "schema_version", "reference_id", "package_sha256", "package_blob",
        "reference_document_record_sha256", "phase3_artifact_occurrence_sha256",
        "components_sha256", "authoritative", "authority_transferred",
        "dispatch_performed", "receipt_sha256",
    }:
        _fail(ReferenceBlockerCode.PACKAGE_INVALID, "receipt fields differ")
    package_hash = _digest(package.get("package_sha256"), "package_sha256")
    if canonical_sha256({k: v for k, v in package.items() if k != "package_sha256"}) != package_hash:
        _fail(ReferenceBlockerCode.PACKAGE_CORRUPT, "package hash differs")
    receipt_hash = _digest(receipt.get("receipt_sha256"), "receipt_sha256")
    if canonical_sha256({k: v for k, v in receipt.items() if k != "receipt_sha256"}) != receipt_hash:
        _fail(ReferenceBlockerCode.PACKAGE_CORRUPT, "receipt hash differs")
    if receipt.get("schema_version") != REFERENCE_PACKAGE_RECEIPT_SCHEMA:
        _fail(ReferenceBlockerCode.PACKAGE_INVALID, "receipt schema differs")
    if package.get("schema_version") != REFERENCE_PACKAGE_SCHEMA:
        _fail(ReferenceBlockerCode.PACKAGE_INVALID, "package schema differs")
    _verify_false_safety(package, "package")
    _verify_false_safety(receipt, "receipt")
    if (
        receipt.get("package_sha256") != package_hash
        or receipt.get("package_blob") != package_fact.as_dict()
        or _sha(package_raw) != package_fact.sha256
    ):
        _fail(ReferenceBlockerCode.PACKAGE_CORRUPT, "receipt package binding differs")
    source = _exact_mapping(
        package.get("source"),
        {"normalized_path", "content_sha256", "byte_length"},
        "source",
    )
    component_mapping = package.get("components")
    if not isinstance(component_mapping, Mapping):
        _fail(ReferenceBlockerCode.PACKAGE_INVALID, "components must be an object")
    raw_component = _blob_fact(component_mapping.get("raw_pdf"), "raw_pdf")
    if (
        _digest(source["content_sha256"], "source.content_sha256")
        != raw_component.sha256
        or source["byte_length"] != raw_component.byte_length
    ):
        _fail(ReferenceBlockerCode.PACKAGE_CORRUPT, "source raw identity differs")
    toolchain = _exact_mapping(
        package.get("toolchain"),
        {"schema_version", "tools", "png_decoder", "options", "toolchain_sha256"},
        "toolchain",
    )
    if toolchain["schema_version"] != REFERENCE_TOOLCHAIN_SCHEMA:
        _fail(ReferenceBlockerCode.PACKAGE_INVALID, "toolchain schema differs")
    if canonical_sha256({k: v for k, v in toolchain.items() if k != "toolchain_sha256"}) != toolchain["toolchain_sha256"]:
        _fail(ReferenceBlockerCode.PACKAGE_CORRUPT, "toolchain hash differs")
    algorithms = _exact_mapping(
        package.get("algorithms"),
        {"schema_version", "text_normalization", "chunking", "rendering", "cas", "algorithms_sha256"},
        "algorithms",
    )
    if algorithms["schema_version"] != REFERENCE_ALGORITHMS_SCHEMA:
        _fail(ReferenceBlockerCode.PACKAGE_INVALID, "algorithms schema differs")
    if canonical_sha256({k: v for k, v in algorithms.items() if k != "algorithms_sha256"}) != algorithms["algorithms_sha256"]:
        _fail(ReferenceBlockerCode.PACKAGE_CORRUPT, "algorithms hash differs")
    occurrence_wire = package.get("phase3_artifact_occurrence")
    occurrence = _materialized_occurrence(
        occurrence_wire,
        cas.get(
            _blob_fact(package["components"]["raw_pdf"], "raw_pdf").blob_ref,
            expected_length=_blob_fact(package["components"]["raw_pdf"], "raw_pdf").byte_length,
            deadline=deadline,
        ),
        str(package.get("source", {}).get("normalized_path", "")),
    )
    if canonical_sha256(occurrence.as_dict()) != package.get("phase3_artifact_occurrence_sha256"):
        _fail(ReferenceBlockerCode.PACKAGE_CORRUPT, "occurrence hash differs")
    try:
        record = verify_reference_document_record(package["reference_document_record"])
    except (ReferenceEvidenceError, KeyError, TypeError) as exc:
        raise ReferenceMaterializationError(
            ReferenceBlockerCode.PACKAGE_CORRUPT, "record does not revalidate"
        ) from exc
    if record.record_sha256 != package.get("reference_document_record_sha256"):
        _fail(ReferenceBlockerCode.PACKAGE_CORRUPT, "record hash differs")
    components = component_mapping
    if (
        not isinstance(components, Mapping)
        or set(components) != {
            "raw_pdf", "record", "page_renders", "page_texts", "chunks",
            "metadata_values",
        }
        or canonical_sha256(components) != package.get("components_sha256")
    ):
        _fail(ReferenceBlockerCode.PACKAGE_CORRUPT, "component set differs")
    record_fact = _blob_fact(components.get("record"), "record")
    if cas.get(record_fact.blob_ref, expected_length=record_fact.byte_length, deadline=deadline) != _canonical_json_bytes(record.as_dict()):
        _fail(ReferenceBlockerCode.PACKAGE_CORRUPT, "record CAS bytes differ")

    expected_groups = {
        "page_renders": [(str(item["page_number"]), item["blob"]) for item in components.get("page_renders", [])],
        "page_texts": [(str(item["page_number"]), item["blob"]) for item in components.get("page_texts", [])],
        "chunks": [(str(item["chunk_id"]), item["blob"]) for item in components.get("chunks", [])],
        "metadata_values": [(str(item["field"]), item["blob"]) for item in components.get("metadata_values", [])],
    }
    for group, items in expected_groups.items():
        seen: set[str] = set()
        for identity, blob_value in items:
            if identity in seen:
                _fail(ReferenceBlockerCode.PACKAGE_CORRUPT, f"duplicate {group} component")
            seen.add(identity)
            fact = _blob_fact(blob_value, f"{group}.{identity}")
            raw = cas.get(fact.blob_ref, expected_length=fact.byte_length, deadline=deadline)
            if group == "page_renders":
                page = record.pages[int(identity) - 1]
                if _sha(raw) != page.render.sha256:
                    _fail(ReferenceBlockerCode.PACKAGE_CORRUPT, "page render differs")
            elif group == "page_texts":
                page = record.pages[int(identity) - 1]
                if raw != page.canonical_text.text.encode("utf-8"):
                    _fail(ReferenceBlockerCode.PACKAGE_CORRUPT, "page text differs")
            elif group == "chunks":
                by_id = {item.chunk_id: item for item in record.chunks}
                if identity not in by_id or raw != by_id[identity].text.encode("utf-8"):
                    _fail(ReferenceBlockerCode.PACKAGE_CORRUPT, "chunk differs")
            else:
                metadata_wire = record.bibliographic_metadata.as_dict()
                if identity not in metadata_wire or raw != canonical_bytes(metadata_wire[identity]):
                    _fail(ReferenceBlockerCode.PACKAGE_CORRUPT, "metadata value differs")
                provenance = {
                    fact.field: fact for fact in record.metadata_provenance
                }
                if (
                    identity not in provenance
                    or provenance[identity].source_ref != fact.blob_ref
                    or provenance[identity].value_sha256
                    != canonical_sha256(metadata_wire[identity])
                ):
                    _fail(ReferenceBlockerCode.PACKAGE_CORRUPT, "metadata provenance differs")
    if len(expected_groups["page_renders"]) != len(record.pages) or len(expected_groups["page_texts"]) != len(record.pages) or len(expected_groups["chunks"]) != len(record.chunks) or len(expected_groups["metadata_values"]) != 4:
        _fail(ReferenceBlockerCode.PACKAGE_CORRUPT, "component coverage differs")
    if (
        receipt.get("reference_document_record_sha256") != record.record_sha256
        or receipt.get("phase3_artifact_occurrence_sha256") != package.get("phase3_artifact_occurrence_sha256")
        or receipt.get("components_sha256") != package.get("components_sha256")
        or receipt.get("reference_id") != package.get("reference_id")
    ):
        _fail(ReferenceBlockerCode.PACKAGE_CORRUPT, "receipt identity differs")
    _check_deadline(deadline, "package replay")
    return MaterializedReferencePackage(package_fact, receipt_fact, package, receipt, record)


def verify_reference_package(
    *,
    cas_root: Path,
    package_blob: Mapping[str, object] | CasBlobFact,
    receipt_blob: Mapping[str, object] | CasBlobFact,
    deadline: object | None = None,
) -> dict[str, object]:
    """Mapping-friendly verifier for CLI/Web/service adapters."""

    return load_reference_package(
        cas_root=cas_root,
        package_blob=package_blob,
        receipt_blob=receipt_blob,
        deadline=deadline,
    ).as_dict()


def structured_reference_unavailable(error: BaseException) -> dict[str, object]:
    """Return a stable path-free unavailable fact for adapter boundaries."""

    if isinstance(error, ReferenceMaterializationError):
        code = error.code
    else:
        code = ReferenceBlockerCode.TOOL_FAILED.value
    return {
        "schema_version": "reference-materialization-unavailable-v1",
        "status": "UNAVAILABLE",
        "reason_code": code,
        "authoritative": False,
        "authority_transferred": False,
        "dispatch_performed": False,
    }


__all__ = (
    "CAS_BLOB_SCHEMA",
    "REFERENCE_PACKAGE_SCHEMA",
    "REFERENCE_PACKAGE_RECEIPT_SCHEMA",
    "CasBlobFact",
    "MaterializedReferencePackage",
    "ReferenceBlockerCode",
    "ReferenceCas",
    "ReferenceMaterializationError",
    "ReferenceMaterializerConfig",
    "load_reference_package",
    "materialize_reference_pdf",
    "read_trusted_pdf",
    "structured_reference_unavailable",
    "verify_reference_package",
)
