#!/usr/bin/env python3
"""Create and verify the exact manifest-bound final submission ZIP."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import zipfile
from pathlib import Path
import sys

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from factory_core.submission_bundle import (
    declared_delivery_files,
    submission_bundle_manifest,
    verify_zip_against_manifest,
)


def iter_bundle_files(project: Path, base: str) -> list[tuple[Path, str]]:
    """Compatibility view backed by the authoritative bundle manifest."""

    project = project.resolve()
    manifest = submission_bundle_manifest(project, base)
    return [
        (project / item["source_path"], item["archive_path"])
        for item in manifest["members"]
    ]


def _atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project")
    parser.add_argument("base")
    parser.add_argument("output")
    args = parser.parse_args()

    project = Path(args.project).resolve()
    output = Path(args.output).resolve()
    if not project.is_dir():
        raise SystemExit(f"Project directory not found: {project}")
    try:
        manifest = submission_bundle_manifest(project, args.base)
    except (OSError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    members = manifest["members"]
    names = {str(item["archive_path"]) for item in members}
    if f"{args.base}_paper.pdf" not in names:
        raise SystemExit("Final PDF was not selected for packaging")
    if not any(name.startswith("models/") for name in names):
        raise SystemExit("No model code selected for packaging")
    if not any(name.startswith("results/") for name in names):
        raise SystemExit("No results selected for packaging")

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.unlink(missing_ok=True)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for item in members:
                archive.write(project / item["source_path"], item["archive_path"])
        verify_zip_against_manifest(temporary, manifest)
        os.replace(temporary, output)
        _atomic_write_json(
            project / ".factory/finalization/submission_bundle_manifest.json",
            manifest,
        )
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    print(
        f"Wrote {output} ({len(members)} files, manifest {manifest['manifest_sha256']})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
