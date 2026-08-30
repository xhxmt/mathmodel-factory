#!/usr/bin/env python3
"""Deterministic candidate archive policy and verification helpers.

The policy is intentionally narrower than general ZIP.  A conforming candidate
contains one top-level directory and regular files only.  Directory entries,
links, devices, encrypted members, ambiguous names, nested archives and
self-referential checksum layouts are rejected.
"""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import unicodedata
import zipfile


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts import evidence_payload_policy as payload_policy  # noqa: E402


MANIFEST_BASENAME = "MANIFEST.json"
CHECKSUMS_RELATIVE = "checksums/SHA256SUMS"
MANIFEST_SCHEMA = "paper-factory-phase4-6-candidate-manifest-v1"
BUILDER_ID = "paper-factory-deterministic-zip-v1"
FIXED_ZIP_TIME = (1980, 1, 1, 0, 0, 0)
DEFAULT_MAX_MEMBER_BYTES = 256 * 1024 * 1024
DEFAULT_MAX_TOTAL_BYTES = 1024 * 1024 * 1024
DEFAULT_MAX_RATIO = 200.0
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_WINDOWS_RESERVED = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
}
_ARCHIVE_SUFFIXES = (
    ".zip",
    ".tar",
    ".tgz",
    ".tar.gz",
    ".tar.bz2",
    ".tar.xz",
    ".gz",
    ".bz2",
    ".xz",
    ".7z",
    ".rar",
)


class ArchivePolicyError(RuntimeError):
    """Raised when an input or archive violates the closed package policy."""


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_member_name(name: str) -> tuple[str, ...]:
    """Return safe canonical components or raise ``ArchivePolicyError``."""

    if not isinstance(name, str) or not name:
        raise ArchivePolicyError("archive member name must be a non-empty string")
    if name != unicodedata.normalize("NFC", name):
        raise ArchivePolicyError(f"non-NFC archive member name: {name!r}")
    if "\\" in name:
        raise ArchivePolicyError(f"backslash archive member name: {name!r}")
    if name.startswith("/") or _DRIVE_RE.match(name):
        raise ArchivePolicyError(f"absolute or drive archive member name: {name!r}")
    if name.endswith("/") or "//" in name:
        raise ArchivePolicyError(f"non-canonical archive member name: {name!r}")
    if len(name.encode("utf-8")) > 4096:
        raise ArchivePolicyError(f"overlong archive member name: {name!r}")
    if any(unicodedata.category(character).startswith("C") for character in name):
        raise ArchivePolicyError(f"control/private archive member name: {name!r}")

    parts = name.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ArchivePolicyError(f"dot or empty archive path component: {name!r}")
    for part in parts:
        if len(part.encode("utf-8")) > 255:
            raise ArchivePolicyError(f"overlong archive path component: {part!r}")
        if part != part.strip() or part.endswith((".", " ")):
            raise ArchivePolicyError(f"ambiguous archive path component: {part!r}")
        if ":" in part:
            raise ArchivePolicyError(f"colon/ADS archive path component: {part!r}")
        stem = part.split(".", 1)[0].casefold()
        if stem in _WINDOWS_RESERVED:
            raise ArchivePolicyError(f"reserved archive path component: {part!r}")

    canonical = PurePosixPath(*parts).as_posix()
    if canonical != name:
        raise ArchivePolicyError(f"non-canonical archive member name: {name!r}")
    return tuple(parts)


def ensure_unique_names(names: list[str]) -> None:
    # ZIP names are a flat list, but extractors materialize every component as
    # a directory.  Validate a component trie so a case-insensitive target
    # cannot merge distinct parent directories (``Foo/a`` vs ``foo/b``), and
    # so one regular member can never also be another member's parent
    # (``a`` vs ``a/b``).
    root: dict[str, object] = {"terminal": None, "children": {}}
    exact: set[str] = set()
    for name in names:
        parts = validate_member_name(name)
        if name in exact:
            raise ArchivePolicyError(f"duplicate archive member: {name!r}")
        exact.add(name)

        node = root
        prefix: list[str] = []
        for part in parts:
            terminal = node["terminal"]
            if terminal is not None:
                raise ArchivePolicyError(
                    f"file/directory prefix conflict: {terminal!r}, {name!r}"
                )
            children = node["children"]
            if not isinstance(children, dict):  # pragma: no cover - internal invariant
                raise ArchivePolicyError("invalid internal archive path trie")
            key = part.casefold()
            entry = children.get(key)
            if entry is None:
                entry = {
                    "spelling": part,
                    "node": {"terminal": None, "children": {}},
                }
                children[key] = entry
            if not isinstance(entry, dict):  # pragma: no cover - internal invariant
                raise ArchivePolicyError("invalid internal archive path trie")
            prior_spelling = entry["spelling"]
            if not isinstance(prior_spelling, str):  # pragma: no cover
                raise ArchivePolicyError("invalid internal archive path trie")
            if prior_spelling != part:
                prior_path = "/".join((*prefix, prior_spelling))
                current_path = "/".join((*prefix, part))
                raise ArchivePolicyError(
                    "casefold-conflicting archive path components: "
                    f"{prior_path!r}, {current_path!r}"
                )
            prefix.append(part)
            child = entry["node"]
            if not isinstance(child, dict):  # pragma: no cover - internal invariant
                raise ArchivePolicyError("invalid internal archive path trie")
            node = child

        prior = node["terminal"]
        if prior is not None:
            raise ArchivePolicyError(
                f"casefold-conflicting archive members: {prior!r}, {name!r}"
            )
        children = node["children"]
        if not isinstance(children, dict):  # pragma: no cover - internal invariant
            raise ArchivePolicyError("invalid internal archive path trie")
        if children:
            raise ArchivePolicyError(
                f"file/directory prefix conflict: {name!r} is a parent member"
            )
        node["terminal"] = name


def _strict_json_loads(raw: bytes, label: str) -> object:
    def reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, item in pairs:
            if key in value:
                raise ArchivePolicyError(f"duplicate JSON key in {label}: {key!r}")
            value[key] = item
        return value

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ArchivePolicyError(f"{label} is not UTF-8") from exc
    try:
        return json.loads(text, object_pairs_hook=reject_duplicate_pairs)
    except json.JSONDecodeError as exc:
        raise ArchivePolicyError(f"invalid JSON in {label}: {exc}") from exc


def parse_checksum_manifest(raw: bytes, label: str) -> dict[str, str]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ArchivePolicyError(f"{label} is not UTF-8") from exc
    if not text.endswith("\n"):
        raise ArchivePolicyError(f"{label} must end with a newline")
    result: dict[str, str] = {}
    folded: dict[str, str] = {}
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line:
            raise ArchivePolicyError(f"blank checksum line in {label}:{line_number}")
        if len(line) < 67 or line[64:66] != "  ":
            raise ArchivePolicyError(f"malformed checksum line in {label}:{line_number}")
        digest = line[:64]
        name = line[66:]
        if not _SHA256_RE.fullmatch(digest):
            raise ArchivePolicyError(f"invalid SHA-256 in {label}:{line_number}")
        validate_member_name(name)
        if name in result:
            raise ArchivePolicyError(f"duplicate checksum path in {label}: {name!r}")
        key = name.casefold()
        if key in folded:
            raise ArchivePolicyError(
                f"casefold-conflicting checksum paths in {label}: "
                f"{folded[key]!r}, {name!r}"
            )
        result[name] = digest
        folded[key] = name
    if list(result) != sorted(result):
        raise ArchivePolicyError(f"{label} paths are not bytewise sorted")
    return result


def _validate_archive_file(path: Path) -> os.stat_result:
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise ArchivePolicyError(f"archive does not exist: {path}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ArchivePolicyError("archive path must be a non-symlink regular file")
    if info.st_nlink != 1:
        raise ArchivePolicyError("archive path must not be a hardlink")
    return info


def _preflight_payload_member_paths(
    member_names: list[str],
) -> tuple[str, str, str]:
    """Apply the central path policy before any ZIP member content is read.

    ZIP metadata is sufficient to establish the unique archive root and the
    two builder-owned closure paths. Only those exact closure paths are
    exempt; every other member is checked as a root-relative payload path.
    Policy denials and implementation failures become closed verifier errors
    before ``ZipFile.open/read/testzip`` can run.
    """

    roots: set[str] = set()
    for name in member_names:
        parts = validate_member_name(name)
        if len(parts) < 2:
            raise ArchivePolicyError("every member must be below one archive root")
        roots.add(parts[0])
    if len(roots) != 1:
        raise ArchivePolicyError("archive must contain exactly one top-level root")

    root = next(iter(roots))
    manifest_name = f"{root}/{MANIFEST_BASENAME}"
    checksums_name = f"{root}/{CHECKSUMS_RELATIVE}"
    members = set(member_names)
    if manifest_name not in members or checksums_name not in members:
        raise ArchivePolicyError("archive lacks the fixed manifest/checksum members")

    fixed_members = {manifest_name, checksums_name}
    root_prefix = f"{root}/"
    for name in member_names:
        if name in fixed_members:
            continue
        relative = name.removeprefix(root_prefix)
        try:
            payload_policy.require_payload_path_allowed(relative)
        except payload_policy.PayloadPolicyEvaluationError as error:
            raise ArchivePolicyError(str(error)) from error
        except BaseException as error:
            raise ArchivePolicyError(
                "payload path policy failed closed before archive member I/O for "
                f"{relative!r}"
            ) from error
    return root, manifest_name, checksums_name


def _verify_outer_checksum(archive: Path, outer: Path, archive_sha256: str) -> None:
    try:
        outer_info = outer.lstat()
    except FileNotFoundError as exc:
        raise ArchivePolicyError(f"outer SHA256SUMS does not exist: {outer}") from exc
    if stat.S_ISLNK(outer_info.st_mode) or not stat.S_ISREG(outer_info.st_mode):
        raise ArchivePolicyError("outer SHA256SUMS must be a non-symlink regular file")
    entries = parse_checksum_manifest(outer.read_bytes(), str(outer))
    expected = {archive.name: archive_sha256}
    if entries != expected:
        raise ArchivePolicyError(
            "outer SHA256SUMS must contain exactly the archive basename and actual hash"
        )


def _validate_manifest(
    manifest: object,
    *,
    root: str,
    actual_hashes: Mapping[str, str],
    actual_sizes: Mapping[str, int],
    actual_modes: Mapping[str, int],
    manifest_name: str,
    checksums_name: str,
) -> list[str]:
    if not isinstance(manifest, dict):
        raise ArchivePolicyError("MANIFEST.json must contain a JSON object")
    expected_keys = {
        "schema",
        "builder",
        "archive_root",
        "deterministic_timestamp",
        "inventory_sha256",
        "metadata",
        "closure",
        "files",
    }
    if set(manifest) != expected_keys:
        raise ArchivePolicyError("MANIFEST.json has unknown or missing top-level keys")
    if manifest["schema"] != MANIFEST_SCHEMA or manifest["builder"] != BUILDER_ID:
        raise ArchivePolicyError("MANIFEST.json schema or builder identity differs")
    if manifest["archive_root"] != root:
        raise ArchivePolicyError("MANIFEST.json archive_root differs")
    if manifest["deterministic_timestamp"] != "1980-01-01T00:00:00Z":
        raise ArchivePolicyError("MANIFEST.json deterministic timestamp differs")
    if not isinstance(manifest["metadata"], dict):
        raise ArchivePolicyError("MANIFEST.json metadata must be an object")
    if not isinstance(manifest["inventory_sha256"], str) or not _SHA256_RE.fullmatch(
        manifest["inventory_sha256"]
    ):
        raise ArchivePolicyError("MANIFEST.json inventory_sha256 is invalid")

    expected_closure = {
        "manifest": manifest_name,
        "checksums": checksums_name,
        "checksums_cover": "every payload member plus MANIFEST.json",
        "checksums_exclude": "checksums/SHA256SUMS (self-reference is forbidden)",
    }
    if manifest["closure"] != expected_closure:
        raise ArchivePolicyError("MANIFEST.json checksum closure declaration differs")

    files = manifest["files"]
    if not isinstance(files, list) or not files:
        raise ArchivePolicyError("MANIFEST.json files must be a non-empty list")
    expected_file_keys = {"source_path", "archive_path", "size", "sha256", "mode"}
    archive_paths: list[str] = []
    source_paths: list[str] = []
    for index, entry in enumerate(files):
        if not isinstance(entry, dict) or set(entry) != expected_file_keys:
            raise ArchivePolicyError(f"invalid MANIFEST.json file entry at index {index}")
        source_path = entry["source_path"]
        archive_path = entry["archive_path"]
        if not isinstance(source_path, str) or not isinstance(archive_path, str):
            raise ArchivePolicyError("manifest paths must be strings")
        validate_member_name(source_path)
        validate_member_name(archive_path)
        if archive_path != f"{root}/{source_path}":
            raise ArchivePolicyError("manifest archive/source path mapping differs")
        if not isinstance(entry["size"], int) or entry["size"] < 0:
            raise ArchivePolicyError("manifest file size is invalid")
        if not isinstance(entry["mode"], int) or entry["mode"] not in {0o644, 0o755}:
            raise ArchivePolicyError("manifest file mode is invalid")
        if not isinstance(entry["sha256"], str) or not _SHA256_RE.fullmatch(
            entry["sha256"]
        ):
            raise ArchivePolicyError("manifest file SHA-256 is invalid")
        if actual_sizes.get(archive_path) != entry["size"]:
            raise ArchivePolicyError(f"manifest size mismatch: {archive_path}")
        if actual_modes.get(archive_path) != entry["mode"]:
            raise ArchivePolicyError(f"manifest mode mismatch: {archive_path}")
        if actual_hashes.get(archive_path) != entry["sha256"]:
            raise ArchivePolicyError(f"manifest SHA-256 mismatch: {archive_path}")
        source_paths.append(source_path)
        archive_paths.append(archive_path)

    if archive_paths != sorted(archive_paths) or source_paths != sorted(source_paths):
        raise ArchivePolicyError("MANIFEST.json file entries are not sorted")
    ensure_unique_names(archive_paths)
    ensure_unique_names(source_paths)
    inventory_bytes = ("\n".join(source_paths) + "\n").encode("utf-8")
    if sha256_bytes(inventory_bytes) != manifest["inventory_sha256"]:
        raise ArchivePolicyError("MANIFEST.json inventory SHA-256 differs")
    return archive_paths


def verify_archive(
    archive: Path,
    *,
    outer_checksums: Path | None = None,
    max_member_bytes: int = DEFAULT_MAX_MEMBER_BYTES,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
    max_ratio: float = DEFAULT_MAX_RATIO,
) -> dict[str, object]:
    """Strictly verify one archive and optionally its outer checksum file."""

    archive = archive.absolute()
    archive_info = _validate_archive_file(archive)
    archive_sha256 = sha256_file(archive)
    actual_hashes: dict[str, str] = {}
    actual_sizes: dict[str, int] = {}
    actual_modes: dict[str, int] = {}
    member_names: list[str] = []
    total_uncompressed = 0
    total_compressed = 0

    try:
        with zipfile.ZipFile(archive, "r") as candidate:
            if candidate.comment:
                raise ArchivePolicyError("archive comment is forbidden")
            infos = candidate.infolist()
            if not infos:
                raise ArchivePolicyError("archive is empty")
            member_names = [info.filename for info in infos]
            ensure_unique_names(member_names)
            root, manifest_name, checksums_name = _preflight_payload_member_paths(
                member_names
            )
            for info in infos:
                name = info.filename
                parts = validate_member_name(name)
                if len(parts) < 2:
                    raise ArchivePolicyError("every member must be below one archive root")
                if PurePosixPath(name).name == archive.name:
                    raise ArchivePolicyError("archive contains a member named after itself")
                lowered = name.casefold()
                if lowered.endswith(_ARCHIVE_SUFFIXES):
                    raise ArchivePolicyError(f"nested archive member is forbidden: {name}")
                if info.is_dir():
                    raise ArchivePolicyError("directory members are forbidden")
                if info.flag_bits & 0x1:
                    raise ArchivePolicyError(f"encrypted archive member: {name}")
                if info.compress_type != zipfile.ZIP_DEFLATED:
                    raise ArchivePolicyError(f"unexpected compression method: {name}")
                if info.date_time != FIXED_ZIP_TIME:
                    raise ArchivePolicyError(f"non-deterministic ZIP timestamp: {name}")
                if info.extra or info.comment:
                    raise ArchivePolicyError(f"extra/comment metadata is forbidden: {name}")
                if info.create_system != 3:
                    raise ArchivePolicyError(f"non-Unix ZIP creator metadata: {name}")
                unix_mode = (info.external_attr >> 16) & 0xFFFF
                if stat.S_IFMT(unix_mode) != stat.S_IFREG:
                    raise ArchivePolicyError(f"non-regular archive member: {name}")
                permissions = stat.S_IMODE(unix_mode)
                if permissions not in {0o644, 0o755}:
                    raise ArchivePolicyError(f"unexpected archive member mode: {name}")
                if info.file_size > max_member_bytes:
                    raise ArchivePolicyError(f"archive member exceeds size limit: {name}")
                if info.file_size and not info.compress_size:
                    raise ArchivePolicyError(f"invalid infinite compression ratio: {name}")
                ratio = info.file_size / max(info.compress_size, 1)
                if ratio > max_ratio:
                    raise ArchivePolicyError(f"archive member exceeds ratio limit: {name}")

                digest = hashlib.sha256()
                prefix = b""
                with candidate.open(info, "r") as handle:
                    while True:
                        chunk = handle.read(1024 * 1024)
                        if not chunk:
                            break
                        if len(prefix) < 8:
                            prefix = (prefix + chunk)[:8]
                        digest.update(chunk)
                if prefix.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
                    raise ArchivePolicyError(f"embedded ZIP content is forbidden: {name}")
                actual_hashes[name] = digest.hexdigest()
                actual_sizes[name] = info.file_size
                actual_modes[name] = permissions
                total_uncompressed += info.file_size
                total_compressed += info.compress_size
                if total_uncompressed > max_total_bytes:
                    raise ArchivePolicyError("archive exceeds total uncompressed size limit")

            bad_crc = candidate.testzip()
            if bad_crc is not None:
                raise ArchivePolicyError(f"CRC failure in archive member: {bad_crc}")

            if manifest_name not in actual_hashes or checksums_name not in actual_hashes:
                raise ArchivePolicyError("archive lacks the fixed manifest/checksum members")
            if actual_sizes[manifest_name] > 8 * 1024 * 1024:
                raise ArchivePolicyError("MANIFEST.json exceeds its size limit")
            if actual_sizes[checksums_name] > 32 * 1024 * 1024:
                raise ArchivePolicyError("internal SHA256SUMS exceeds its size limit")

            manifest_raw = candidate.read(manifest_name)
            checksums_raw = candidate.read(checksums_name)
            manifest = _strict_json_loads(manifest_raw, manifest_name)
            payload_paths = _validate_manifest(
                manifest,
                root=root,
                actual_hashes=actual_hashes,
                actual_sizes=actual_sizes,
                actual_modes=actual_modes,
                manifest_name=manifest_name,
                checksums_name=checksums_name,
            )
            expected_members = set(payload_paths) | {manifest_name, checksums_name}
            if set(member_names) != expected_members:
                missing = sorted(expected_members - set(member_names))
                extra = sorted(set(member_names) - expected_members)
                raise ArchivePolicyError(
                    f"archive/manifest closure mismatch; missing={missing}, extra={extra}"
                )
            checksums = parse_checksum_manifest(checksums_raw, checksums_name)
            expected_checksum_paths = set(payload_paths) | {manifest_name}
            if set(checksums) != expected_checksum_paths:
                missing = sorted(expected_checksum_paths - set(checksums))
                extra = sorted(set(checksums) - expected_checksum_paths)
                raise ArchivePolicyError(
                    f"internal checksum closure mismatch; missing={missing}, extra={extra}"
                )
            if checksums_name in checksums:
                raise ArchivePolicyError("internal SHA256SUMS illegally covers itself")
            for name, expected_digest in checksums.items():
                if actual_hashes[name] != expected_digest:
                    raise ArchivePolicyError(f"internal checksum mismatch: {name}")
    except zipfile.BadZipFile as exc:
        raise ArchivePolicyError(f"invalid ZIP or CRC: {exc}") from exc

    after = archive.lstat()
    before_fingerprint = (
        archive_info.st_dev,
        archive_info.st_ino,
        archive_info.st_size,
        archive_info.st_mtime_ns,
    )
    after_fingerprint = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if before_fingerprint != after_fingerprint:
        raise ArchivePolicyError("archive changed while it was being verified")
    if outer_checksums is not None:
        _verify_outer_checksum(archive, outer_checksums.absolute(), archive_sha256)

    return {
        "archive": str(archive),
        "bytes": archive_info.st_size,
        "sha256": archive_sha256,
        "members": len(member_names),
        "payload_members": len(member_names) - 2,
        "uncompressed_bytes": total_uncompressed,
        "compressed_bytes": total_compressed,
        "crc": "PASS",
        "path_policy": "PASS",
        "special_file_policy": "regular-files-only; PASS",
        "manifest_checksum_closure": "PASS",
        "outer_sha256sums": "PASS" if outer_checksums is not None else "NOT_CHECKED",
    }
