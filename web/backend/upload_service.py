from __future__ import annotations

import tarfile
import zipfile
from pathlib import Path


class ArchiveTraversalError(RuntimeError):
    pass


def _safe_target(root: Path, name: str) -> Path:
    resolved_root = root.resolve()
    target = (resolved_root / name).resolve()
    if target != resolved_root and resolved_root not in target.parents:
        raise ArchiveTraversalError(f"archive member escapes root: {name}")
    return target


def extract_archive(archive_path: Path, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    if zipfile.is_zipfile(archive_path):
        with zipfile.ZipFile(archive_path) as zf:
            for member in zf.infolist():
                _safe_target(out_dir, member.filename)
            zf.extractall(out_dir)
        return

    with tarfile.open(archive_path, "r:*") as tf:
        for member in tf.getmembers():
            _safe_target(out_dir, member.name)
            if member.issym() or member.islnk():
                raise ArchiveTraversalError(f"links not allowed: {member.name}")
        tf.extractall(out_dir)


def find_problem_file(root: Path) -> Path | None:
    preferred: list[Path] = []
    fallback: list[Path] = []

    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() not in {".pdf", ".md"}:
            continue
        fallback.append(path)
        lowered = path.name.lower()
        if any(key in lowered for key in ("problem", "question", "题目", "题")):
            preferred.append(path)

    if preferred:
        return preferred[0]
    if fallback:
        return fallback[0]
    return None
