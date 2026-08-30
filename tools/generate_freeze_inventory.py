#!/usr/bin/env python3
"""Generate and validate the explicit Phase 4-6 source freeze inventory."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
ARCHIVE_TOOLS = REPOSITORY_ROOT / "archive_tools"
sys.path.insert(0, str(REPOSITORY_ROOT))
sys.path.insert(0, str(ARCHIVE_TOOLS))
from archive_safety import ensure_unique_names, validate_member_name  # noqa: E402
from scripts import evidence_payload_policy as payload_policy  # noqa: E402


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_new(path: Path, value: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())


def _git_paths(root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=root,
        check=True,
        stdout=subprocess.PIPE,
    )
    paths = result.stdout.decode("utf-8").split("\0")
    if paths[-1:] == [""]:
        paths.pop()
    return paths


def generate(source_root: Path, output_dir: Path) -> dict[str, object]:
    """Generate one inventory with the central path policy before file I/O."""

    root = source_root.resolve(strict=True)
    if not root.is_dir():
        raise SystemExit("source root must be a directory")
    output = output_dir
    output.mkdir(mode=0o700, parents=True, exist_ok=False)

    all_paths = sorted(_git_paths(root))
    selected: list[str] = []
    excluded: list[tuple[str, str]] = []
    for relative in all_paths:
        try:
            finding = payload_policy.evaluate_payload_path(relative)
        except payload_policy.PayloadPolicyEvaluationError as error:
            raise SystemExit(
                f"payload path policy failed closed: {relative!r}"
            ) from error
        if finding is not None:
            excluded.append(
                (relative, f"path-policy-before-file-io:{finding.rule}")
            )
            continue
        parts = validate_member_name(relative)
        if not parts:
            raise SystemExit(f"candidate path has no components: {relative}")
        selected.append(relative)

    if not selected:
        raise SystemExit("candidate inventory is empty")
    ensure_unique_names(selected)
    if selected != sorted(selected):
        raise SystemExit("candidate inventory is not bytewise sorted")

    mode_size_lines = ["path\tsize\tmode\tsha256"]
    checksum_lines: list[str] = []
    total_bytes = 0
    for relative in selected:
        path = root / relative
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise SystemExit(f"candidate input is a symlink: {relative}")
        if not stat.S_ISREG(info.st_mode):
            raise SystemExit(f"candidate input is not a regular file: {relative}")
        if info.st_nlink != 1:
            raise SystemExit(f"candidate input is a hardlink: {relative}")
        digest = _sha256_file(path)
        normalized_mode = 0o755 if info.st_mode & 0o111 else 0o644
        total_bytes += info.st_size
        mode_size_lines.append(
            f"{relative}\t{info.st_size}\t{normalized_mode:04o}\t{digest}"
        )
        checksum_lines.append(f"{digest}  {relative}")

    paths_raw = ("\n".join(selected) + "\n").encode("utf-8")
    inventory_raw = ("\n".join(mode_size_lines) + "\n").encode("utf-8")
    checksums_raw = ("\n".join(checksum_lines) + "\n").encode("utf-8")
    exclusions_raw = (
        "path\treason\n"
        + "\n".join(f"{path}\t{reason}" for path, reason in excluded)
        + "\n"
    ).encode("utf-8")
    summary = {
        "all_git_visible_paths": len(all_paths),
        "excluded_path_count": len(excluded),
        "exclusion_policy": {
            "authority": (
                "scripts.evidence_payload_policy.payload_path_finding"
            ),
            "decision_stage": "before-lstat-open-read",
        },
        "inventory_file_sha256": _sha256_bytes(inventory_raw),
        "inventory_path_count": len(selected),
        "inventory_paths_sha256": _sha256_bytes(paths_raw),
        "source_root": str(root),
        "source_sha256sums_sha256": _sha256_bytes(checksums_raw),
        "total_source_bytes": total_bytes,
    }
    _write_new(output / "CANDIDATE_SOURCE_PATHS.txt", paths_raw)
    _write_new(output / "CANDIDATE_SOURCE_FILE_INVENTORY.tsv", inventory_raw)
    _write_new(output / "CANDIDATE_SOURCE_SHA256SUMS", checksums_raw)
    _write_new(output / "CANDIDATE_EXCLUSIONS.tsv", exclusions_raw)
    _write_new(
        output / "CANDIDATE_INVENTORY_SUMMARY.json",
        (json.dumps(summary, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    generate(args.source_root, args.output_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
