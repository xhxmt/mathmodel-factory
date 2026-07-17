#!/usr/bin/env python3
"""Fingerprint the exact final-judge packets and the PDF they approve."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.judge_packet import packet_fingerprints
from scripts.model_dispatch_config import get_model_entry, get_step_model_ids


FINGERPRINT_VERSION = 3
EVALUATOR_CONTRACT_VERSION = 1
FACTORY_ROOT = Path(__file__).resolve().parents[1]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _contained_file(project: Path, path: Path) -> bool:
    """Reject symlinks that would make the final hash read outside project."""

    if not path.is_file():
        return False
    try:
        path.resolve(strict=True).relative_to(project)
    except (OSError, ValueError):
        return False
    return True


def submission_files(project: Path, base: str) -> list[Path]:
    """Return final-paper assets whose delivery meaning is not fully textual.

    Role packet fingerprints below are the authoritative judge-input binding.
    Keeping the bibliography, tables, and figures explicit additionally makes
    the final contract invalidate when a referenced binary asset changes even
    when that asset is represented only by a filename in the paper context.
    """

    project = project.resolve()
    candidates: list[Path] = []
    for paper in (project / f"{base}_paper.tex", project / "paper" / "paper.tex"):
        if _contained_file(project, paper):
            candidates.append(paper)
            break
    references = project / "references.bib"
    if _contained_file(project, references):
        candidates.append(references)
    for folder_name in ("tables", "figures"):
        folder = project / folder_name
        if not folder.is_dir():
            continue
        candidates.extend(
            path
            for path in folder.rglob("*")
            if _contained_file(project, path)
            and "archive" not in path.relative_to(project).parts
        )
    return sorted(set(candidates), key=lambda path: path.relative_to(project).as_posix())


def _file_record(project: Path, path: Path) -> dict[str, object]:
    return {
        "path": path.relative_to(project).as_posix(),
        "size": path.stat().st_size,
        "sha256": _sha256(path),
    }


def _versioned_file_record(root: Path, relative: str) -> dict[str, object]:
    path = root / relative
    exists = path.is_file()
    return {
        "path": relative,
        "exists": exists,
        "sha256": _sha256(path) if exists else None,
    }


def evaluator_contract_payload(base: str, factory_root: Path | None = None) -> dict[str, object]:
    """Return the exact Step-13 evaluator implementation and model selection.

    Final-judge cache validity depends on more than paper inputs: changing a
    role prompt, aggregation rule, packet builder, caller, or registry routing
    changes the evaluator itself and must invalidate an earlier PASS.
    """

    root = (factory_root or FACTORY_ROOT).resolve()
    config_path = root / "web/model_config.json"
    registry_path = root / "web/model_registry.json"
    assignment = get_step_model_ids(config_path, base, 13)
    selection_source = "model_config" if assignment else "builtin_default"
    primary, fallback = assignment or ("deepseek-chat", "")
    selected: dict[str, object] = {
        "primary_id": primary,
        "fallback_id": fallback or None,
        "primary": get_model_entry(registry_path, primary),
        "fallback": get_model_entry(registry_path, fallback) if fallback else None,
    }
    implementation_files = (
        "run_paper.sh",
        "scripts/judge_packet.py",
        "scripts/aggregate_judges.py",
        "scripts/llm_judge_call.py",
        "scripts/api_agent_run.py",
        "scripts/model_dispatch_config.py",
    )
    prompt_files = (
        "prompts/judges/math_auditor.txt",
        "prompts/judges/execution_auditor.txt",
        "prompts/judges/paper_reviewer.txt",
    )
    return {
        "version": EVALUATOR_CONTRACT_VERSION,
        "role_schema": "judge-role-v1",
        "aggregate_schema": "judge-aggregate-v1",
        "implementation": {
            relative: _versioned_file_record(root, relative)
            for relative in implementation_files
        },
        "prompts": {
            relative: _versioned_file_record(root, relative)
            for relative in prompt_files
        },
        "model_dispatch": {
            "selection_source": selection_source,
            "selection": selected,
            "config": _versioned_file_record(root, "web/model_config.json"),
            "registry": _versioned_file_record(root, "web/model_registry.json"),
        },
    }


def _pdf_record(project: Path, base: str) -> dict[str, object]:
    pdf = project / f"{base}_paper.pdf"
    if not _contained_file(project, pdf):
        return {"path": pdf.name, "exists": False, "size": 0, "sha256": None}
    return {
        "path": pdf.name,
        "exists": True,
        "size": pdf.stat().st_size,
        "sha256": _sha256(pdf),
    }


def submission_fingerprint_payload(project: Path, base: str | None = None) -> dict[str, object]:
    """Build the current, side-effect-free final-review identity payload."""

    project = project.resolve()
    resolved_base = base or project.name
    return {
        "version": FINGERPRINT_VERSION,
        "base": resolved_base,
        "judge_packet_fingerprints": packet_fingerprints(project, resolved_base),
        "evaluator_contract": evaluator_contract_payload(resolved_base),
        "submission_assets": [
            _file_record(project, path) for path in submission_files(project, resolved_base)
        ],
        "reviewed_pdf": _pdf_record(project, resolved_base),
    }


def submission_fingerprint(project: Path, base: str | None = None) -> str:
    payload = submission_fingerprint_payload(project, base)
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def final_judge_is_current(project: Path, base: str | None = None) -> bool:
    project = project.resolve()
    resolved_base = base or project.name
    pdf = project / f"{resolved_base}_paper.pdf"
    hash_file = project / "judge_outputs" / "final_submission.sha256"
    if not hash_file.is_file() or not _contained_file(project, pdf) or pdf.stat().st_size <= 0:
        return False
    expected = hash_file.read_text(encoding="utf-8", errors="replace").strip()
    return bool(expected) and expected == submission_fingerprint(project, resolved_base)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project")
    parser.add_argument("--base")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    project = Path(args.project)
    try:
        if args.check:
            return 0 if final_judge_is_current(project, args.base) else 1
        if args.json:
            print(json.dumps(submission_fingerprint_payload(project, args.base), ensure_ascii=False, indent=2))
        else:
            print(submission_fingerprint(project, args.base))
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
