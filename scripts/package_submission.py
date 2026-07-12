#!/usr/bin/env python3
"""Create a Modeling Factory submission bundle.

The bundle is intentionally reproducibility-oriented: final PDF, LaTeX source,
problem brief, model code, results, figures, tables, references, and audit logs.
It excludes runner traces, caches, temporary files, and raw MinerU sidecars unless
they are needed as ordinary source files.
"""

from __future__ import annotations

import argparse
import os
import zipfile
from pathlib import Path


SKIP_DIR_NAMES = {
    ".git",
    ".pytest_cache",
    "__pycache__",
    ".runner.lock",
    "runner_snapshots",
    "source.mineru",
}

SKIP_SUFFIXES = {
    ".aux",
    ".bbl",
    ".blg",
    ".log",
    ".out",
    ".toc",
    ".fls",
    ".fdb_latexmk",
    ".synctex.gz",
    ".pyc",
}

TOP_LEVEL_FILES = {
    "abstract_draft.md",
    "assumption_ledger.md",
    "audit_issue_ledger.md",
    "chosen_method.md",
    "citation_audit.md",
    "code_review.md",
    "derobotification.md",
    "evaluation.md",
    "judge_evaluation.md",
    "method_decision.md",
    "model.md",
    "modeling_guide.md",
    "references.bib",
    "research_brief.md",
    "review_comments.md",
    "revision_summary.md",
    "sensitivity_report.md",
    "solve_log.md",
    "symbol_table.md",
    "viability_gate.md",
    "viable_streams.md",
    "visualization_log.md",
}

INCLUDE_DIRS = {
    "data/raw",
    "figures",
    "models",
    "paper",
    "problem",
    "results",
    "scripts",
    "style",
    "tables",
}


def should_skip(path: Path, rel: Path) -> bool:
    if rel.parts[:2] == ("paper", "archive"):
        return True
    if any(part in SKIP_DIR_NAMES for part in rel.parts):
        return True
    if path.name.startswith(".runner") or path.name in {".heartbeat", ".killed", ".review_state.json"}:
        return True
    if path.suffix.lower() in SKIP_SUFFIXES:
        return True
    if path.name.endswith("~") or path.name.startswith(".#"):
        return True
    return False


def should_include(path: Path, rel: Path, base: str) -> bool:
    rel_posix = rel.as_posix()
    if rel.name in {f"{base}_paper.pdf", f"{base}_paper.tex"}:
        return True
    if rel.parent == Path(".") and (rel.name in TOP_LEVEL_FILES or rel.name.startswith("m") and rel.suffix in {".md", ".json", ".csv"}):
        return True
    for dirname in INCLUDE_DIRS:
        if rel_posix == dirname or rel_posix.startswith(dirname + "/"):
            return True
    return False


def iter_bundle_files(project: Path, base: str) -> list[tuple[Path, str]]:
    files: list[tuple[Path, str]] = []
    for root, dirs, names in os.walk(project):
        root_path = Path(root)
        rel_root = root_path.relative_to(project)
        dirs[:] = [dirname for dirname in dirs if dirname not in SKIP_DIR_NAMES]
        for name in names:
            path = root_path / name
            rel = rel_root / name if rel_root != Path(".") else Path(name)
            if should_skip(path, rel) or not should_include(path, rel, base):
                continue
            files.append((path, rel.as_posix()))
    return sorted(files, key=lambda item: item[1])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", help="Project directory, usually complete/<base> or ongoing/<base>.")
    parser.add_argument("base", help="Project base name.")
    parser.add_argument("output", help="Output zip path.")
    args = parser.parse_args()

    project = Path(args.project).resolve()
    output = Path(args.output).resolve()
    base = args.base

    if not project.is_dir():
        raise SystemExit(f"Project directory not found: {project}")
    pdf = project / f"{base}_paper.pdf"
    if not pdf.is_file():
        raise SystemExit(f"Final PDF not found: {pdf}")

    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_suffix(output.suffix + ".tmp")
    if tmp.exists():
        tmp.unlink()

    files = iter_bundle_files(project, base)
    if not any(arc == f"{base}_paper.pdf" for _path, arc in files):
        raise SystemExit(f"Final PDF was not selected for packaging: {pdf}")
    if not any(arc.startswith("models/") for _path, arc in files):
        raise SystemExit("No model code selected for packaging")
    if not any(arc.startswith("results/") for _path, arc in files):
        raise SystemExit("No results selected for packaging")

    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path, arcname in files:
            zf.write(path, arcname)

    with zipfile.ZipFile(tmp) as zf:
        bad = zf.testzip()
        if bad:
            raise SystemExit(f"Zip integrity check failed at member: {bad}")

    tmp.replace(output)
    print(f"Wrote {output} ({len(files)} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
