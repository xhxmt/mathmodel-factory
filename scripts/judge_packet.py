#!/usr/bin/env python3
"""Build isolated, hash-addressed input packets for independent judges."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


SELF_AUTHORED_STATUS = {
    "code_review.md",
    "delivery_manifest.json",
    "entry_gate.md",
    "evaluation.md",
    "final_delivery_evaluation.md",
    "judge_evaluation.md",
    "modeling_scope_gate.md",
    "pre_delivery_evaluation.md",
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
PACKET_VERSION = 2
COMPLETENESS_CONTRACT_VERSION = "judge-packet-completeness-v1"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resolve_project_file(project: Path, path: Path) -> tuple[Path | None, str | None]:
    """Resolve a candidate without allowing packet reads outside ``project``."""
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        return None, "unresolvable_path"
    try:
        resolved.relative_to(project)
    except ValueError:
        return None, "outside_project_root"
    if not resolved.is_file():
        return None, "not_regular_file"
    return resolved, None


def _project_files(project: Path) -> list[Path]:
    ignored_roots = {"judge_packets", "judge_outputs", ".git", "evaluation", "evaluations"}
    ignored_parts = {"archive", "archives"}
    return sorted(
        (
            path
            for path in project.rglob("*")
            if path.is_file()
            and path.relative_to(project).parts[0] not in ignored_roots
            and not any(part.lower() in ignored_parts for part in path.relative_to(project).parts)
            and path.name not in SELF_AUTHORED_STATUS
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
        "numbers_manifest.json",
        "claim_ledger.json",
    }


def _execution_priority(project: Path, path: Path) -> tuple[int, str]:
    relative = path.relative_to(project).as_posix()
    name = path.name
    if name == "claim_ledger.json":
        priority = 0
    elif relative.endswith("_paper.tex") or relative == "paper/paper.tex":
        priority = 1
    elif relative.startswith("results/"):
        priority = 2
    elif name.endswith(("_verification.latest.json", "_verification.latest.txt")):
        priority = 3
    elif name in {
        "solve_log.md",
        "sensitivity_report.md",
        "numbers_manifest.json",
    }:
        priority = 4
    elif relative.startswith("logs/"):
        priority = 5
    elif relative.startswith("models/"):
        priority = 6
    else:
        priority = 7
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
        if (
            _is_execution_evidence(path.relative_to(project).as_posix())
            or path.relative_to(project).as_posix() in paper_names
        )
    ]
    execution.sort(key=lambda path: _execution_priority(project, path))
    return {"paper": paper, "math": math, "execution": execution}


def _relative(project: Path, path: Path) -> str:
    return path.relative_to(project).as_posix()


def _first_path(
    project: Path, paths: list[Path], predicate
) -> str | None:
    for path in paths:
        relative = _relative(project, path)
        if predicate(relative):
            return relative
    return None


def _role_requirements(
    project: Path, role: str, paths: list[Path], base_name: str
) -> list[dict[str, object]]:
    """Declare the minimum *complete* evidence needed to judge ``role``.

    Each requirement deliberately selects one deterministic primary artifact.
    This avoids making an optional appendix or a large secondary code file a
    hard blocker while still preventing a truncated core artifact from being
    silently treated as sufficient evidence.
    """

    final_paper = _first_path(
        project,
        paths,
        lambda relative: relative in {f"{base_name}_paper.tex", "paper/paper.tex"},
    )
    problem = _first_path(project, paths, _is_problem_file)

    def requirement(identifier: str, description: str, path: str | None) -> dict[str, object]:
        return {
            "id": identifier,
            "description": description,
            "required_status": "included",
            "paths": [path] if path else [],
        }

    if role == "paper":
        return [
            requirement("final_paper", "final paper text", final_paper),
            requirement("problem_statement", "primary problem statement", problem),
        ]

    if role == "math":
        exposition = _first_path(project, paths, lambda relative: relative == "model.md")
        exposition = exposition or final_paper
        return [
            requirement("problem_statement", "primary problem statement", problem),
            requirement("final_paper", "final paper text containing mathematical claims", final_paper),
            requirement(
                "mathematical_exposition",
                "primary mathematical exposition (model.md, otherwise final paper)",
                exposition,
            ),
        ]

    primary_result = _first_path(
        project,
        paths,
        lambda relative: relative == "results/canonical_results.json",
    )
    primary_result = primary_result or _first_path(
        project,
        paths,
        lambda relative: relative.startswith("results/")
        and "canonical" in Path(relative).name.lower(),
    )
    primary_result = primary_result or _first_path(
        project, paths, lambda relative: relative.startswith("results/")
    )
    solver_suffixes = {".py", ".r", ".jl", ".m", ".c", ".cc", ".cpp", ".java", ".sh"}
    solver_name_markers = (
        "02_model", "03_solve", "solve", "solver", "optimi", "algorithm", "main", "model",
    )
    implementation = _first_path(
        project,
        paths,
        lambda relative: relative.startswith("models/")
        and Path(relative).suffix.lower() in solver_suffixes
        and any(marker in Path(relative).stem.lower() for marker in solver_name_markers),
    )
    implementation = implementation or _first_path(
        project,
        paths,
        lambda relative: relative.startswith("models/")
        and Path(relative).suffix.lower() in solver_suffixes,
    )
    execution_trace = _first_path(
        project,
        paths,
        lambda relative: Path(relative).name.endswith(
            ("_verification.latest.json", "_verification.latest.txt")
        ),
    )
    execution_trace = execution_trace or _first_path(
        project, paths, lambda relative: relative == "solve_log.md"
    )
    execution_trace = execution_trace or _first_path(
        project, paths, lambda relative: relative.startswith("logs/")
    )
    execution_trace = execution_trace or _first_path(
        project,
        paths,
        lambda relative: relative.startswith("models/")
        and Path(relative).suffix.lower() == ".log",
    )
    return [
        requirement("final_paper", "final paper text containing reported claims", final_paper),
        requirement("primary_results", "canonical or primary machine-readable results", primary_result),
        requirement("implementation", "primary model implementation", implementation),
        requirement("execution_trace", "solver or verification execution evidence", execution_trace),
    ]


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


def _render_context(
    project: Path, paths: list[Path], requirements: list[dict[str, object]]
) -> tuple[str, list[dict]]:
    chunks: list[str] = []
    files: list[dict] = []
    used = 0
    critical_for: dict[str, list[str]] = {}
    for requirement in requirements:
        for relative in requirement["paths"]:
            normalized = str(relative)
            critical_for.setdefault(normalized, []).append(str(requirement["id"]))
    for path in paths:
        relative = path.relative_to(project).as_posix()
        item = {"path": relative}
        resolved, omission_reason = _resolve_project_file(project, path)
        if resolved is None:
            item.update({"status": "omitted", "reason": omission_reason})
            files.append(item)
            continue
        item.update({
            "sha256": _sha256(resolved),
            "size": resolved.stat().st_size,
        })
        if path.suffix.lower() not in TEXT_SUFFIXES:
            item.update({"status": "omitted", "reason": "unsupported_non_text"})
            files.append(item)
            continue
        original = resolved.read_text(encoding="utf-8", errors="replace")
        original_size = len(original.encode("utf-8"))
        header = f"\n----- FILE: {relative} -----\n"
        # A role's primary evidence is all-or-nothing.  It may use the whole
        # context budget, but is never silently middle-truncated.  Secondary
        # code and appendices retain the per-file cap and are disclosed below.
        if relative in critical_for:
            text = original
        else:
            framing_bytes = len((header + "\n").encode("utf-8"))
            remaining = max(0, MAX_CONTEXT_BYTES - used - framing_bytes)
            text = _truncate_text(original, min(MAX_FILE_BYTES, remaining))
        encoded_size = len((header + text + "\n").encode("utf-8"))
        if not text or used + encoded_size > MAX_CONTEXT_BYTES:
            item.update({"status": "omitted", "reason": "context_byte_limit"})
            files.append(item)
            continue
        chunks.extend((header, text, "\n"))
        used += encoded_size
        included_size = len(text.encode("utf-8"))
        was_truncated = relative not in critical_for and included_size < original_size
        item.update({
            "status": "truncated" if was_truncated else "included",
            "included_bytes": included_size,
        })
        if was_truncated:
            item["reason"] = (
                "per_file_byte_limit"
                if original_size > MAX_FILE_BYTES and included_size >= MAX_FILE_BYTES
                else "remaining_context_byte_limit"
            )
        files.append(item)
    if any(item["status"] == "omitted" for item in files):
        omitted_marker = "\n----- SOME SELECTED FILES OMITTED; SEE PACKET MANIFEST -----\n"
    else:
        omitted_marker = ""
    if omitted_marker and used + len(omitted_marker.encode("utf-8")) <= MAX_CONTEXT_BYTES:
        chunks.append(omitted_marker)
    return "".join(chunks), files


def _completeness(files: list[dict], requirements: list[dict[str, object]]) -> dict:
    by_path = {str(item["path"]): item for item in files}
    evaluated: list[dict[str, object]] = []
    for declared in requirements:
        item = dict(declared)
        paths = [str(path) for path in item["paths"]]
        satisfied_paths = [
            path for path in paths if by_path.get(path, {}).get("status") == "included"
        ]
        item["satisfied_paths"] = satisfied_paths
        item["satisfied"] = bool(paths) and len(satisfied_paths) == len(paths)
        if not paths:
            item["failure_reason"] = "required_artifact_missing"
        elif not item["satisfied"]:
            item["failure_reason"] = "required_artifact_not_fully_included"
        evaluated.append(item)

    critical_paths = {
        str(path) for requirement in requirements for path in requirement["paths"]
    }
    limitations = [
        {
            "path": str(item["path"]),
            "status": str(item["status"]),
            "reason": str(item.get("reason") or "unspecified"),
            "critical": str(item["path"]) in critical_paths,
        }
        for item in files
        if item["status"] in {"truncated", "omitted"}
    ]
    complete = all(bool(item["satisfied"]) for item in evaluated)
    return {
        "contract_version": COMPLETENESS_CONTRACT_VERSION,
        "status": "COMPLETE" if complete else "INCOMPLETE",
        "eligible": complete,
        "requirements": evaluated,
        "limitations": limitations,
    }


def _manifest(
    project: Path,
    role: str,
    files: list[dict],
    context: str,
    requirements: list[dict[str, object]],
) -> dict:
    status_counts = {
        status: sum(item["status"] == status for item in files)
        for status in ("included", "truncated", "omitted")
    }
    manifest = {
        "version": PACKET_VERSION,
        "role": role,
        "project": project.name,
        "status_counts": status_counts,
        "limits": {
            "context_bytes": MAX_CONTEXT_BYTES,
            "per_file_bytes": MAX_FILE_BYTES,
            "critical_file_policy": "include_in_full_or_mark_role_incomplete",
        },
        "context": {
            "sha256": hashlib.sha256(context.encode("utf-8")).hexdigest(),
            "size": len(context.encode("utf-8")),
        },
        "completeness": _completeness(files, requirements),
        "files": files,
    }
    canonical = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    manifest["packet_fingerprint"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return manifest


def _atomic_write(path: Path, data: str) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(data, encoding="utf-8")
    temporary.replace(path)


def packet_payloads(project: Path, base_name: str | None = None) -> dict[str, dict]:
    """Return the exact role packet payloads without mutating the project.

    Keeping packet construction pure lets workflow freshness checks recompute
    the inputs that a judge *would* receive.  They must not trust an old
    ``judge_packets/*/manifest.json`` after model code, results, or execution
    evidence has changed.
    """

    project = project.resolve()
    if not project.is_dir():
        raise FileNotFoundError(f"project directory not found: {project}")
    base_name = base_name or project.name
    selected = _selected_paths(project, base_name)
    result: dict[str, dict] = {}
    for role, paths in selected.items():
        requirements = _role_requirements(project, role, paths, base_name)
        context, files = _render_context(project, paths, requirements)
        result[role] = {
            "context": context,
            "manifest": _manifest(project, role, files, context, requirements),
        }
    return result


def packet_fingerprints(project: Path, base_name: str | None = None) -> dict[str, str]:
    """Compute current role packet fingerprints directly from source files."""

    return {
        role: str(payload["manifest"]["packet_fingerprint"])
        for role, payload in packet_payloads(project, base_name).items()
    }


def build_packets(project: Path, base_name: str | None = None) -> dict[str, dict]:
    project = project.resolve()
    payloads = packet_payloads(project, base_name)
    result: dict[str, dict] = {}
    for role, payload in payloads.items():
        packet_dir = project / "judge_packets" / role
        packet_dir.mkdir(parents=True, exist_ok=True)
        context = str(payload["context"])
        manifest = payload["manifest"]
        _atomic_write(
            packet_dir / "manifest.json",
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        )
        _atomic_write(packet_dir / "context.txt", context)
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
