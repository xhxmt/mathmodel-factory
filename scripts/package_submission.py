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
from factory_core.phase9_delivery_fence import require_phase9_delivery_authority


_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


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


def _write_deterministic_member(
    archive: zipfile.ZipFile, source: Path, archive_path: str
) -> None:
    info = zipfile.ZipInfo(archive_path, date_time=_ZIP_TIMESTAMP)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    info.flag_bits |= 0x800
    archive.writestr(info, source.read_bytes())


def package_submission(
    project: str | Path,
    base: str,
    output: str | Path,
    *,
    workflow_id: str,
    run_generation: str,
) -> dict[str, object]:
    """Build one submission only after an independent live Authority check.

    This producer deliberately performs the fence check itself.  A caller's
    prior audit, cached decision, or release check is not authority for this
    filesystem mutation boundary.
    """

    project = Path(project).resolve()
    output = Path(output).resolve()
    if not project.is_dir():
        raise ValueError(f"Project directory not found: {project}")

    # This must precede manifest construction and every directory/file write.
    require_phase9_delivery_authority(
        project,
        workflow_id=workflow_id,
        run_generation=run_generation,
    )

    manifest = submission_bundle_manifest(project, base)
    members = manifest["members"]
    names = {str(item["archive_path"]) for item in members}
    if f"{base}_paper.pdf" not in names:
        raise ValueError("Final PDF was not selected for packaging")
    if not any(name.startswith("models/") for name in names):
        raise ValueError("No model code selected for packaging")
    if not any(name.startswith("results/") for name in names):
        raise ValueError("No results selected for packaging")

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.unlink(missing_ok=True)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for item in members:
                _write_deterministic_member(
                    archive,
                    project / item["source_path"],
                    item["archive_path"],
                )
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
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project")
    parser.add_argument("base")
    parser.add_argument("output")
    parser.add_argument("--workflow-id", required=True)
    parser.add_argument("--run-generation", required=True)
    args = parser.parse_args()

    try:
        package_submission(
            args.project,
            args.base,
            args.output,
            workflow_id=args.workflow_id,
            run_generation=args.run_generation,
        )
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
