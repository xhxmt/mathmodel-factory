from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


FINAL_INPUT_MANIFEST_SCHEMA = "factory-final-input-manifest-v1"


class FinalizationSnapshotChanged(RuntimeError):
    def __init__(self, changed_paths: list[str]):
        self.changed_paths = changed_paths
        super().__init__(
            "finalization input snapshot changed: " + ", ".join(changed_paths[:8])
        )


@dataclass(frozen=True)
class FinalInputSnapshot:
    fingerprint: str
    manifest_path: Path
    manifest: dict[str, Any]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical_hash(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _input_paths(project: Path) -> list[Path]:
    from scripts.submission_fingerprint import submission_files

    paths = set(submission_files(project, project.name))
    for relative in (
        "problem/problem_brief.md",
        "problem/problem_plan.json",
        "problem/deliverables.json",
        "chosen_method.md",
        "model.md",
        "symbol_table.md",
        "assumption_ledger.md",
        "claim_registry.json",
        "quality_contract.json",
        "solve_log.md",
        "results/canonical_results.json",
    ):
        path = project / relative
        if path.is_file() and not path.is_symlink():
            paths.add(path)
    for root_name in ("models", "results", "data/final"):
        root = project / root_name
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if (
                path.is_file()
                and not path.is_symlink()
                and "__pycache__" not in path.parts
                and path.suffix != ".pyc"
            ):
                paths.add(path)
    return sorted(paths, key=lambda path: path.relative_to(project).as_posix())


def build_final_input_manifest(project_dir: str | Path) -> FinalInputSnapshot:
    project = Path(project_dir).resolve()
    files = [
        {
            "path": path.relative_to(project).as_posix(),
            "bytes": path.stat().st_size,
            "sha256": _sha256(path),
        }
        for path in _input_paths(project)
    ]
    identity = {
        "schema_version": FINAL_INPUT_MANIFEST_SCHEMA,
        "base": project.name,
        "files": files,
    }
    fingerprint = _canonical_hash(identity)
    manifest = {**identity, "fingerprint": fingerprint}
    path = project / ".factory" / "finalization" / "input_manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(manifest, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return FinalInputSnapshot(fingerprint, path, manifest)


def verify_final_input_snapshot(
    project_dir: str | Path, snapshot: FinalInputSnapshot
) -> None:
    project = Path(project_dir).resolve()
    current_records = {
        path.relative_to(project).as_posix(): {
            "path": path.relative_to(project).as_posix(),
            "bytes": path.stat().st_size,
            "sha256": _sha256(path),
        }
        for path in _input_paths(project)
    }
    expected_records = {
        str(item["path"]): item for item in snapshot.manifest.get("files", [])
    }
    changed = sorted(
        path
        for path in set(current_records) | set(expected_records)
        if current_records.get(path) != expected_records.get(path)
    )
    identity = {
        "schema_version": FINAL_INPUT_MANIFEST_SCHEMA,
        "base": project.name,
        "files": [current_records[path] for path in sorted(current_records)],
    }
    if _canonical_hash(identity) != snapshot.fingerprint and not changed:
        changed = ["<manifest-identity>"]
    if changed:
        raise FinalizationSnapshotChanged(changed)


def reopen_after_for_changed_paths(paths: list[str]) -> int:
    target = 13
    for relative in paths:
        lowered = relative.lower()
        if lowered.startswith("problem/"):
            target = min(target, -1)
        elif lowered.startswith("models/") or lowered in {
            "model.md",
            "symbol_table.md",
            "assumption_ledger.md",
            "claim_registry.json",
            "quality_contract.json",
            "chosen_method.md",
        }:
            target = min(target, 3)
        elif lowered.startswith(("results/", "data/final/")) or lowered == "solve_log.md":
            target = min(target, 4)
        elif lowered.startswith("figures/"):
            target = min(target, 7)
        elif lowered.endswith(".tex") and not lowered.startswith("tables/"):
            target = min(target, 10)
        elif lowered.endswith(".bib") or lowered.startswith("tables/"):
            target = min(target, 13)
        else:
            target = min(target, 3)
    return target
