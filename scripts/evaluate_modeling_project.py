#!/usr/bin/env python3
"""Evaluate a Modeling Factory project from file-state evidence.

The runner remains authoritative for workflow progress. This script layers a
small delivery-quality checklist on top so benchmark runs can fail fast when a
project is missing critical artifacts.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


METHOD_PATH_RE = re.compile(r"method_library/[A-Za-z0-9_./-]+\.md")
VERDICT_RE = re.compile(r"^VERDICT:\s*(\S+)", re.M)


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    severity: str = "error"


@dataclass
class Evaluation:
    project: str
    base: str
    inferred_step: int | None
    checks: list[Check] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(check.ok or check.severity == "warning" for check in self.checks)

    def add(self, name: str, ok: bool, detail: str, severity: str = "error") -> None:
        self.checks.append(Check(name, ok, detail, severity))


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="utf-8", errors="replace")


def infer_step(root: Path, project: Path) -> tuple[int | None, str]:
    runner = root / "run_paper.sh"
    try:
        proc = subprocess.run(
            [str(runner), "--infer-step", str(project)],
            cwd=root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )
    except Exception as exc:  # pragma: no cover - defensive for local ops
        return None, str(exc)
    text = (proc.stdout or proc.stderr).strip()
    match = re.search(r"-?\d+", text)
    if proc.returncode == 0 and match:
        return int(match.group(0)), text
    return None, text or f"runner exited {proc.returncode}"


def line_count(path: Path) -> int:
    if not path.is_file():
        return 0
    return len(read_text(path).splitlines())


def file_exists(path: Path, min_lines: int = 1) -> bool:
    return path.is_file() and line_count(path) >= min_lines


def nonempty_files(path: Path, patterns: tuple[str, ...]) -> list[Path]:
    if not path.is_dir():
        return []
    found: list[Path] = []
    for pattern in patterns:
        found.extend(p for p in path.rglob(pattern) if p.is_file() and p.stat().st_size > 0)
    return sorted(set(found))


def first_verdict(path: Path) -> str | None:
    if not path.is_file():
        return None
    match = VERDICT_RE.search(read_text(path))
    return match.group(1) if match else None


def method_paths_in(project: Path) -> set[str]:
    candidates = [
        project / "problem" / "candidate_methods.md",
        project / "viable_streams.md",
        project / "chosen_method.md",
        project / "method_decision.md",
    ]
    paths: set[str] = set()
    for path in candidates:
        if path.is_file():
            paths.update(METHOD_PATH_RE.findall(read_text(path)))
    return {path for path in paths if "..." not in path}


def zip_ok(path: Path) -> tuple[bool, str]:
    if not path.is_file():
        return False, "missing"
    try:
        with zipfile.ZipFile(path) as zf:
            bad = zf.testzip()
            names = zf.namelist()
    except zipfile.BadZipFile:
        return False, "bad zip file"
    if bad:
        return False, f"corrupt member: {bad}"
    return True, f"{len(names)} members"


def evaluate(project: Path, root: Path) -> Evaluation:
    project = project.resolve()
    base = project.name
    step, infer_detail = infer_step(root, project)
    ev = Evaluation(project=str(project), base=base, inferred_step=step)

    ev.add("infer_step", step == 16, f"{infer_detail} (expected 16)")

    required_problem = [
        "source.md",
        "problem_brief.md",
        "terminology_table.md",
        "data_inventory.md",
        "feasibility_constraints.md",
        "candidate_methods.md",
    ]
    if (project / "problem" / "method_retrieval.md").is_file():
        required_problem.append("method_retrieval.md")
    missing_problem = [name for name in required_problem if not (project / "problem" / name).is_file()]
    ev.add("problem_artifacts", not missing_problem, "missing: " + ", ".join(missing_problem) if missing_problem else "ok")

    for name in [
        "research_brief.md",
        "viable_streams.md",
        "viability_gate.md",
        "method_decision.md",
        "chosen_method.md",
        "model.md",
        "symbol_table.md",
        "assumption_ledger.md",
        "solve_log.md",
        "sensitivity_report.md",
        "evaluation.md",
        "visualization_log.md",
        "code_review.md",
        "review_comments.md",
        "revision_summary.md",
        "judge_evaluation.md",
        "abstract_draft.md",
        "citation_audit.md",
        "derobotification.md",
    ]:
        ev.add(f"artifact:{name}", file_exists(project / name), "present" if (project / name).is_file() else "missing")

    gate1_text = read_text(project / "code_review.md") if (project / "code_review.md").is_file() else ""
    ev.add(
        "gate1_numeric_trace",
        bool(gate1_text.strip()) and not re.search(r"\b(BLOCKING|UNRESOLVED)\b", gate1_text, re.I),
        "code_review exists and has no obvious unresolved marker" if gate1_text.strip() else "code_review missing/empty",
    )

    verdict = first_verdict(project / "judge_evaluation.md")
    ev.add("gate2_verdict", verdict == "PASS", f"VERDICT: {verdict or 'missing'}")

    methods = method_paths_in(project)
    missing_methods = sorted(path for path in methods if not (root / path).is_file())
    ev.add(
        "method_paths",
        bool(methods) and not missing_methods,
        f"{len(methods)} method paths, missing: {', '.join(missing_methods)}" if missing_methods else f"{len(methods)} method paths verified",
    )

    result_files = nonempty_files(project / "results", ("*.json", "*.csv", "*.log", "*.pdf", "*.png"))
    log_files = nonempty_files(project / "logs", ("*.log", "*.json", "*.txt"))
    ev.add("results_present", bool(result_files), f"{len(result_files)} nonempty result files")
    ev.add("logs_present", bool(log_files), f"{len(log_files)} nonempty log files")

    paper_tex = project / f"{base}_paper.tex"
    paper_pdf = project / f"{base}_paper.pdf"
    papers_pdf = root / "papers" / f"{base}_paper.pdf"
    submission_zip = root / "papers" / f"{base}_submission.zip"
    ev.add("paper_tex", paper_tex.is_file() and "ABSTRACT_PLACEHOLDER" not in read_text(paper_tex), "present without placeholder" if paper_tex.is_file() else "missing")
    ev.add("project_pdf", paper_pdf.is_file() and paper_pdf.stat().st_size > 0, str(paper_pdf))
    ev.add("papers_pdf", papers_pdf.is_file() and papers_pdf.stat().st_size > 0, str(papers_pdf))
    ok_zip, zip_detail = zip_ok(submission_zip)
    ev.add("submission_zip", ok_zip, zip_detail)

    return ev


def as_json(ev: Evaluation) -> dict[str, Any]:
    return {
        "project": ev.project,
        "base": ev.base,
        "inferred_step": ev.inferred_step,
        "passed": ev.passed,
        "checks": [check.__dict__ for check in ev.checks],
    }


def print_text(ev: Evaluation) -> None:
    status = "PASS" if ev.passed else "FAIL"
    print(f"Evaluation: {status}")
    print(f"Project: {ev.project}")
    print(f"Inferred step: {ev.inferred_step}")
    print()
    for check in ev.checks:
        marker = "OK" if check.ok else ("WARN" if check.severity == "warning" else "FAIL")
        print(f"[{marker}] {check.name}: {check.detail}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", help="Path to complete/<base> or ongoing/<base> project directory.")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    args = parser.parse_args()

    root = repo_root()
    project = Path(args.project)
    if not project.is_dir():
        print(f"Project directory not found: {project}", file=sys.stderr)
        return 2

    ev = evaluate(project, root)
    if args.json:
        print(json.dumps(as_json(ev), ensure_ascii=False, indent=2))
    else:
        print_text(ev)
    return 0 if ev.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
