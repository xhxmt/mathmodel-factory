#!/usr/bin/env python3
"""Build a deterministic, self-verifying Phase 4-6 candidate ZIP.

The source tree is opened read-only.  The output must be a new absolute
directory, so the command cannot overwrite an earlier candidate.  Payload is
selected only through an explicit newline-delimited inventory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import zipfile


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from archive_tools.archive_safety import (  # noqa: E402
    ArchivePolicyError,
    BUILDER_ID,
    CHECKSUMS_RELATIVE,
    FIXED_ZIP_TIME,
    MANIFEST_BASENAME,
    MANIFEST_SCHEMA,
    ensure_unique_names,
    parse_checksum_manifest,
    sha256_bytes,
    validate_member_name,
    verify_archive,
)
from scripts import evidence_payload_policy as payload_policy  # noqa: E402


def _strict_json(raw: bytes, label: str) -> dict[str, object]:
    def reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, item in pairs:
            if key in value:
                raise ArchivePolicyError(f"duplicate JSON key in {label}: {key!r}")
            value[key] = item
        return value

    try:
        parsed = json.loads(raw.decode("utf-8"), object_pairs_hook=reject_duplicate_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ArchivePolicyError(f"invalid UTF-8 JSON metadata: {label}") from exc
    if not isinstance(parsed, dict):
        raise ArchivePolicyError("build metadata must be a JSON object")
    return parsed


def _contains_freeze_placeholder(value: object) -> bool:
    if isinstance(value, str):
        return "REPLACE_AT_FREEZE" in value
    if isinstance(value, list):
        return any(_contains_freeze_placeholder(item) for item in value)
    if isinstance(value, dict):
        return any(
            _contains_freeze_placeholder(key) or _contains_freeze_placeholder(item)
            for key, item in value.items()
        )
    return False


def _read_inventory(path: Path) -> tuple[list[str], bytes]:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ArchivePolicyError("inventory must be a non-symlink regular file")
    if info.st_size > 16 * 1024 * 1024:
        raise ArchivePolicyError("inventory exceeds 16 MiB")
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ArchivePolicyError("inventory is not UTF-8") from exc
    if "\r" in text or not text.endswith("\n"):
        raise ArchivePolicyError("inventory must use LF and end with a newline")
    paths = text.splitlines()
    if not paths or any(not item for item in paths):
        raise ArchivePolicyError("inventory must be non-empty and contain no blank lines")
    for relative in paths:
        _enforce_payload_path_before_io(relative)
    ensure_unique_names(paths)
    if paths != sorted(paths):
        raise ArchivePolicyError("inventory paths must be bytewise sorted")
    return paths, raw


def _reject_reserved_inventory_paths(paths: list[str]) -> None:
    """Reject payload names that collide with builder-owned closure members."""

    # Reuse the component-trie policy so exact, casefold and file/directory
    # prefix aliases all fail the same way.  Backslash spellings and other
    # non-canonical names have already been rejected by ``_read_inventory``.
    ensure_unique_names([*paths, MANIFEST_BASENAME, CHECKSUMS_RELATIVE])


def _open_source_root(path: Path) -> int:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ArchivePolicyError("source root must be a non-symlink directory")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return os.open(path, flags)


def _enforce_payload_path_before_io(relative: str) -> None:
    try:
        payload_policy.require_payload_path_allowed(relative)
    except payload_policy.PayloadPolicyEvaluationError as error:
        raise ArchivePolicyError(str(error)) from error


def _read_regular_at(root_fd: int, relative: str) -> tuple[bytes, tuple[int, ...], int]:
    _enforce_payload_path_before_io(relative)
    parts = validate_member_name(relative)
    current = os.dup(root_fd)
    descriptor: int | None = None
    try:
        for component in parts[:-1]:
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            next_descriptor = os.open(component, flags, dir_fd=current)
            os.close(current)
            current = next_descriptor
        flags = os.O_RDONLY | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(parts[-1], flags, dir_fd=current)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ArchivePolicyError(f"payload is not a regular file: {relative}")
        if before.st_nlink != 1:
            raise ArchivePolicyError(f"payload is a hardlink: {relative}")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
        fingerprint = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
            stat.S_IMODE(before.st_mode),
        )
        after_fingerprint = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
            stat.S_IMODE(after.st_mode),
        )
        if fingerprint != after_fingerprint:
            raise ArchivePolicyError(f"payload changed while being read: {relative}")
        mode = 0o755 if before.st_mode & 0o111 else 0o644
        return b"".join(chunks), fingerprint, mode
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(current)


def _zip_info(name: str, mode: int) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(filename=name, date_time=FIXED_ZIP_TIME)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | mode) << 16
    info.extra = b""
    info.comment = b""
    return info


def _canonical_json(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )


def _validate_output(output_dir: Path, source_root: Path, archive_name: str) -> None:
    if not output_dir.is_absolute():
        raise ArchivePolicyError("output directory must be an absolute path")
    if output_dir.exists() or output_dir.is_symlink():
        raise ArchivePolicyError("output directory already exists; overwrite is forbidden")
    parent = output_dir.parent
    parent_info = parent.lstat()
    if stat.S_ISLNK(parent_info.st_mode) or not stat.S_ISDIR(parent_info.st_mode):
        raise ArchivePolicyError("output parent must be a non-symlink directory")
    if Path(archive_name).name != archive_name or not archive_name.endswith(".zip"):
        raise ArchivePolicyError("archive name must be one safe .zip basename")
    validate_member_name(archive_name)
    archive_path = output_dir / archive_name
    try:
        archive_path.relative_to(source_root)
    except ValueError:
        return
    # Output below the source tree is allowed only because it does not exist and
    # the explicit inventory is read before creation.  Equality is never safe.
    if archive_path == source_root:
        raise ArchivePolicyError("archive output cannot replace the source root")


def build(args: argparse.Namespace) -> dict[str, object]:
    source_root = Path(args.source_root).absolute()
    inventory_path = Path(args.inventory).absolute()
    metadata_path = Path(args.metadata).absolute()
    output_dir = Path(args.output_dir)
    archive_root = args.archive_root
    validate_member_name(archive_root)
    if "/" in archive_root:
        raise ArchivePolicyError("archive root must be exactly one path component")
    _validate_output(output_dir, source_root, args.archive_name)

    source_paths, inventory_raw = _read_inventory(inventory_path)
    for relative in source_paths:
        _enforce_payload_path_before_io(relative)
    _reject_reserved_inventory_paths(source_paths)
    missing_required = sorted(set(args.require) - set(source_paths))
    if missing_required:
        raise ArchivePolicyError(f"required source paths are absent: {missing_required}")
    metadata_info = metadata_path.lstat()
    if stat.S_ISLNK(metadata_info.st_mode) or not stat.S_ISREG(metadata_info.st_mode):
        raise ArchivePolicyError("metadata must be a non-symlink regular file")
    metadata = _strict_json(metadata_path.read_bytes(), str(metadata_path))
    if _contains_freeze_placeholder(metadata):
        raise ArchivePolicyError("build metadata still contains REPLACE_AT_FREEZE")

    root_fd = _open_source_root(source_root)
    payload: dict[str, tuple[bytes, tuple[int, ...], int]] = {}
    total_bytes = 0
    try:
        for relative in source_paths:
            value = _read_regular_at(root_fd, relative)
            total_bytes += len(value[0])
            if total_bytes > args.max_total_bytes:
                raise ArchivePolicyError("payload exceeds configured total byte limit")
            payload[relative] = value

        manifest_files: list[dict[str, object]] = []
        for relative in source_paths:
            data, _fingerprint, mode = payload[relative]
            manifest_files.append(
                {
                    "source_path": relative,
                    "archive_path": f"{archive_root}/{relative}",
                    "size": len(data),
                    "sha256": sha256_bytes(data),
                    "mode": mode,
                }
            )
        manifest_name = f"{archive_root}/{MANIFEST_BASENAME}"
        checksums_name = f"{archive_root}/{CHECKSUMS_RELATIVE}"
        manifest = {
            "schema": MANIFEST_SCHEMA,
            "builder": BUILDER_ID,
            "archive_root": archive_root,
            "deterministic_timestamp": "1980-01-01T00:00:00Z",
            "inventory_sha256": sha256_bytes(inventory_raw),
            "metadata": metadata,
            "closure": {
                "manifest": manifest_name,
                "checksums": checksums_name,
                "checksums_cover": "every payload member plus MANIFEST.json",
                "checksums_exclude": "checksums/SHA256SUMS (self-reference is forbidden)",
            },
            "files": manifest_files,
        }
        manifest_raw = _canonical_json(manifest)
        checksum_values = {
            entry["archive_path"]: entry["sha256"] for entry in manifest_files
        }
        checksum_values[manifest_name] = sha256_bytes(manifest_raw)
        checksums_raw = "".join(
            f"{checksum_values[name]}  {name}\n" for name in sorted(checksum_values)
        ).encode("utf-8")

        output_dir.mkdir(mode=0o700)
        partial = output_dir / f".{args.archive_name}.partial"
        outer_partial = output_dir / ".SHA256SUMS.partial"
        archive = output_dir / args.archive_name
        outer = output_dir / "SHA256SUMS"
        try:
            with zipfile.ZipFile(
                partial,
                "x",
                compression=zipfile.ZIP_DEFLATED,
                compresslevel=9,
                strict_timestamps=True,
            ) as candidate:
                candidate.comment = b""
                for relative in source_paths:
                    data, _fingerprint, mode = payload[relative]
                    candidate.writestr(
                        _zip_info(f"{archive_root}/{relative}", mode), data
                    )
                candidate.writestr(_zip_info(manifest_name, 0o644), manifest_raw)
                candidate.writestr(_zip_info(checksums_name, 0o644), checksums_raw)

            # Global freeze check: every input must have the same identity and
            # bytes after the archive has been written.
            for relative in source_paths:
                second_data, second_fingerprint, second_mode = _read_regular_at(
                    root_fd, relative
                )
                first_data, first_fingerprint, first_mode = payload[relative]
                if (
                    second_fingerprint != first_fingerprint
                    or second_mode != first_mode
                    or sha256_bytes(second_data) != sha256_bytes(first_data)
                ):
                    raise ArchivePolicyError(
                        f"source tree changed during candidate build: {relative}"
                    )
            # Strictly validate only the hidden temporary archive.  A failed
            # build or validation must never publish a final-named ZIP.
            verification = verify_archive(
                partial,
                max_total_bytes=args.max_total_bytes + len(manifest_raw) + len(checksums_raw),
            )
            archive_sha256 = str(verification["sha256"])
            outer_fd = os.open(
                outer_partial,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
                0o600,
            )
            try:
                os.write(
                    outer_fd,
                    f"{archive_sha256}  {archive.name}\n".encode("utf-8"),
                )
                os.fsync(outer_fd)
            finally:
                os.close(outer_fd)

            outer_entries = parse_checksum_manifest(
                outer_partial.read_bytes(), str(outer_partial)
            )
            if outer_entries != {archive.name: archive_sha256}:
                raise ArchivePolicyError("temporary outer SHA256SUMS differs")

            # Publish the checksum first, then atomically publish the already
            # verified ZIP as the final operation.  If ZIP publication fails,
            # cleanup removes the harmless orphan checksum and no final-named
            # candidate is visible to downstream consumers.
            os.replace(outer_partial, outer)
            os.replace(partial, archive)
            output_fd = os.open(output_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            try:
                os.fsync(output_fd)
            finally:
                os.close(output_fd)

            published = dict(verification)
            published["archive"] = str(archive.absolute())
            published["outer_sha256sums"] = "PASS"
            return published
        except BaseException as primary:
            for residue in (partial, outer_partial, archive, outer):
                try:
                    residue.unlink(missing_ok=True)
                except BaseException as cleanup_error:
                    try:
                        primary.add_note(
                            "archive cleanup failure for "
                            f"{residue.name}: {type(cleanup_error).__name__}: "
                            f"{cleanup_error}"
                        )
                    except BaseException:
                        pass
            raise
    finally:
        os.close(root_fd)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--inventory", required=True)
    parser.add_argument("--metadata", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--archive-name", required=True)
    parser.add_argument("--archive-root", default="paper_factory_phase4_6_candidate")
    parser.add_argument(
        "--require",
        action="append",
        default=[],
        help="inventory path that must be present; repeat for multiple gates",
    )
    parser.add_argument("--max-total-bytes", type=int, default=1024 * 1024 * 1024)
    return parser.parse_args()


def main() -> int:
    try:
        result = build(parse_args())
    except (ArchivePolicyError, OSError, ValueError) as exc:
        print(f"BUILD_REJECTED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
