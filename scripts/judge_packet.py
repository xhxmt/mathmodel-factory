#!/usr/bin/env python3
"""Build isolated, hash-addressed input packets for independent judges."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


SELF_AUTHORED_EVALUATION = {
    "evaluation.md",
    "judge_evaluation.md",
    "review_comments.md",
    "revision_summary.md",
}
TEXT_SUFFIXES = {
    ".c",
    ".cc",
    ".cpp",
    ".csv",
    ".json",
    ".jl",
    ".log",
    ".m",
    ".md",
    ".py",
    ".r",
    ".sh",
    ".tex",
    ".txt",
    ".yaml",
    ".yml",
}
MAX_CONTEXT_BYTES = 180_000
MAX_FILE_BYTES = 55_000


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _project_files(project: Path) -> list[Path]:
    ignored_roots = {"judge_packets", "judge_outputs", ".git"}
    return sorted(
        (
            path
            for path in project.rglob("*")
            if path.is_file()
            and not any(part in ignored_roots for part in path.relative_to(project).parts)
        ),
        key=lambda path: path.relative_to(project).as_posix(),
    )


def _is_problem_file(relative: str) -> bool:
    return relative.startswith("problem/") and Path(relative).suffix.lower() in TEXT_SUFFIXES


def _is_model_code(relative: str) -> bool:
    path = Path(relative)
    return (
        relative.startswith("models/")
        or relative.startswith("tests/")
    ) and path.suffix.lower() in TEXT_SUFFIXES and path.suffix.lower() != ".log"


def _is_execution_evidence(relative: str) -> bool:
    name = Path(relative).name
    if relative.startswith("logs/"):
        return Path(relative).suffix.lower() == ".log" and not name.startswith("step_") and name != "runner.log"
    if relative.startswith("results/") and Path(relative).suffix.lower() in TEXT_SUFFIXES:
        return True
    if relative.startswith("models/") and Path(relative).suffix.lower() in TEXT_SUFFIXES:
        return True
    if name.endswith(("_verification.latest.json", "_verification.latest.txt")):
        return True
    return name in {
        "solve_log.md",
        "sensitivity_report.md",
        "code_review.md",
        "numbers_manifest.json",
        "delivery_manifest.json",
        "quality_contract.json",
    }


def _delivery_manifest_matches_project(path: Path, base_name: str) -> bool:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    project = payload.get("project")
    if isinstance(project, dict):
        return project.get("base") == base_name
    return project == base_name


def _execution_priority(project: Path, path: Path) -> tuple[int, str]:
    relative = path.relative_to(project).as_posix()
    name = path.name
    if relative.startswith("results/"):
        priority = 0
    elif name.endswith(("_verification.latest.json", "_verification.latest.txt")):
        priority = 1
    elif name in {
        "solve_log.md",
        "sensitivity_report.md",
        "code_review.md",
        "numbers_manifest.json",
        "delivery_manifest.json",
        "quality_contract.json",
    }:
        priority = 2
    elif relative.startswith("logs/"):
        priority = 3
    elif relative.startswith("models/"):
        priority = 4
    else:
        priority = 5
    return priority, relative


def _paper_priority(project: Path, path: Path, base_name: str) -> tuple[int, str]:
    relative = path.relative_to(project).as_posix()
    if relative in {f"{base_name}_paper.tex", "paper/paper.tex"}:
        priority = 0
    elif relative in {"problem/problem_brief.md", "problem/source.md"}:
        priority = 1
    else:
        priority = 2
    return priority, relative


def _math_priority(project: Path, path: Path, base_name: str) -> tuple[int, str]:
    relative = path.relative_to(project).as_posix()
    if relative in {"problem/problem_brief.md", "problem/source.md"}:
        priority = 0
    elif relative == "model.md":
        priority = 1
    elif relative in {f"{base_name}_paper.tex", "paper/paper.tex"}:
        priority = 2
    elif relative in {
        "symbol_table.md",
        "assumption_ledger.md",
        "chosen_method.md",
        "quality_contract.json",
    }:
        priority = 3
    elif relative.startswith("models/"):
        priority = 4
    else:
        priority = 5
    return priority, relative


def _selected_paths(project: Path, base_name: str) -> dict[str, list[Path]]:
    files = _project_files(project)
    paper_names = {f"{base_name}_paper.tex", "paper/paper.tex"}
    math_names = {
        "model.md",
        "symbol_table.md",
        "assumption_ledger.md",
        "chosen_method.md",
        "quality_contract.json",
    }

    paper = [
        path
        for path in files
        if path.relative_to(project).as_posix() in paper_names
        or _is_problem_file(path.relative_to(project).as_posix())
    ]
    paper = [
        path for path in paper if path.relative_to(project).as_posix() not in SELF_AUTHORED_EVALUATION
    ]
    paper.sort(key=lambda path: _paper_priority(project, path, base_name))

    math = [
        path
        for path in files
        if _is_problem_file(path.relative_to(project).as_posix())
        or path.relative_to(project).as_posix() in math_names | paper_names
        or _is_model_code(path.relative_to(project).as_posix())
    ]
    math.sort(key=lambda path: _math_priority(project, path, base_name))

    execution = [
        path
        for path in files
        if _is_execution_evidence(path.relative_to(project).as_posix())
        and (
            path.relative_to(project).as_posix() != "delivery_manifest.json"
            or _delivery_manifest_matches_project(path, base_name)
        )
    ]
    execution.sort(key=lambda path: _execution_priority(project, path))
    return {"paper": paper, "math": math, "execution": execution}


def _manifest(project: Path, role: str, paths: list[Path]) -> dict:
    return {
        "version": 1,
        "role": role,
        "project": project.name,
        "files": [
            {
                "path": path.relative_to(project).as_posix(),
                "sha256": _sha256(path),
                "size": path.stat().st_size,
            }
            for path in paths
        ],
    }


def _truncate_text(text: str, byte_limit: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= byte_limit:
        return text
    marker = b"\n... [middle truncated for judge packet budget] ...\n"
    available = max(0, byte_limit - len(marker))
    head_size = available * 3 // 4
    tail_size = available - head_size
    head = encoded[:head_size].decode("utf-8", errors="ignore")
    tail = encoded[-tail_size:].decode("utf-8", errors="ignore") if tail_size else ""
    return head + marker.decode() + tail


def _write_context(project: Path, paths: list[Path], output: Path) -> None:
    chunks: list[str] = []
    used = 0
    truncated = False
    for path in paths:
        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        text = _truncate_text(
            path.read_text(encoding="utf-8", errors="replace"),
            MAX_FILE_BYTES,
        )
        header = f"\n----- FILE: {path.relative_to(project).as_posix()} -----\n"
        encoded_size = len((header + text + "\n").encode("utf-8"))
        if used + encoded_size > MAX_CONTEXT_BYTES:
            truncated = True
            continue
        chunks.extend((header, text, "\n"))
        used += encoded_size
    omitted_marker = "\n----- SOME FILES OMITTED AT DETERMINISTIC SIZE LIMIT -----\n"
    if truncated and used + len(omitted_marker.encode("utf-8")) <= MAX_CONTEXT_BYTES:
        chunks.append(omitted_marker)
    output.write_text("".join(chunks), encoding="utf-8")


def build_packets(project: Path, base_name: str | None = None) -> dict[str, dict]:
    project = project.resolve()
    if not project.is_dir():
        raise FileNotFoundError(f"project directory not found: {project}")
    base_name = base_name or project.name
    selected = _selected_paths(project, base_name)
    result: dict[str, dict] = {}
    for role, paths in selected.items():
        packet_dir = project / "judge_packets" / role
        packet_dir.mkdir(parents=True, exist_ok=True)
        manifest = _manifest(project, role, paths)
        (packet_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        _write_context(project, paths, packet_dir / "context.txt")
        result[role] = manifest
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project")
    parser.add_argument("--base")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        manifests = build_packets(Path(args.project), args.base)
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}")
        return 2
    if args.json:
        print(json.dumps(manifests, ensure_ascii=False, indent=2))
    else:
        for role, manifest in manifests.items():
            print(f"{role}: {len(manifest['files'])} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
