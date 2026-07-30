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

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import workflow_state


METHOD_PATH_RE = re.compile(r"method_library/[A-Za-z0-9_./-]+\.md")
INCOMPLETE_RESULT_STATUSES = {"RUNNING", "PARTIAL", "PENDING", "INCOMPLETE", "FAILED", "ERROR"}


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


def run_python_check(root: Path, args: list[str], timeout: int = 60) -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            [sys.executable, *args],
            cwd=root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except Exception as exc:  # pragma: no cover - defensive for local ops
        return False, str(exc)
    detail = "\n".join(part.strip() for part in (proc.stdout, proc.stderr) if part.strip())
    return proc.returncode == 0, detail[:1000] if detail else f"exit {proc.returncode}"


def symbol_check_ok(root: Path, project: Path, base: str) -> tuple[bool, str]:
    ok, detail = run_python_check(root, ["scripts/verify_symbols.py", str(project), base])
    if ok:
        return True, "verify_symbols PASS"

    used_match = re.search(r"SYMBOLS_USED\s*=\s*(\d+)", detail)
    undefined_match = re.search(r"UNDEFINED_SYMBOLS\s*=\s*(\d+)", detail)
    if not used_match or not undefined_match:
        return False, detail or "verify_symbols failed"
    used = int(used_match.group(1))
    undefined = int(undefined_match.group(1))
    coverage = 1 - undefined / used if used else 0
    code_review = project / "code_review.md"
    documented = code_review.is_file() and re.search(
        r"未登记符号|undefined_symbols|UNDEFINED_SYMBOLS|白名单|补登记|symbol",
        read_text(code_review),
        re.I,
    )
    if coverage >= 0.5 and documented:
        return True, f"verify_symbols WARNING documented; coverage={coverage:.3f}, undefined={undefined}"
    return False, f"verify_symbols FAIL; coverage={coverage:.3f}, undefined={undefined}"


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


def read_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def numeric_leaf_paths(data: Any, prefix: str = "") -> list[str]:
    paths: list[str] = []
    if isinstance(data, bool):
        return paths
    if isinstance(data, (int, float)):
        if prefix and prefix.rsplit(".", 1)[-1] != "problem":
            paths.append(prefix)
        return paths
    if isinstance(data, dict):
        for key, value in data.items():
            child = f"{prefix}.{key}" if prefix else str(key)
            paths.extend(numeric_leaf_paths(value, child))
    elif isinstance(data, list):
        for idx, value in enumerate(data):
            child = f"{prefix}[{idx}]" if prefix else f"[{idx}]"
            paths.extend(numeric_leaf_paths(value, child))
    return paths


def canonical_results_state(project: Path) -> tuple[bool, str]:
    canonical = project / "results" / "canonical_results.json"
    if canonical.is_file():
        try:
            data = read_json(canonical)
        except Exception as exc:
            return False, f"canonical_results.json parse error: {exc}"
        numeric_paths = numeric_leaf_paths(data)
        if not numeric_paths:
            return False, "canonical_results.json has no numeric result values"
        text = json.dumps(data, ensure_ascii=False).upper()
        for status in INCOMPLETE_RESULT_STATUSES:
            if f'"{status}"' in text:
                return False, f"canonical_results.json contains incomplete status {status}"
        return True, f"canonical_results.json with {len(numeric_paths)} numeric leaves"

    result_root = project / "results"
    values_files = sorted(result_root.glob("p*/values.json")) if result_root.is_dir() else []
    if not values_files:
        return False, "missing results/canonical_results.json and results/p*/values.json"

    incomplete: list[str] = []
    complete_count = 0
    for path in values_files:
        rel = path.relative_to(project)
        try:
            data = read_json(path)
        except Exception as exc:
            incomplete.append(f"{rel}: parse error {exc}")
            continue
        status = str(data.get("status", "")).upper() if isinstance(data, dict) else ""
        numeric_paths = numeric_leaf_paths(data)
        if status in INCOMPLETE_RESULT_STATUSES:
            incomplete.append(f"{rel}: status={status}")
        elif not numeric_paths:
            incomplete.append(f"{rel}: no key numeric results")
        else:
            complete_count += 1

    if incomplete:
        return False, "; ".join(incomplete[:5])
    return True, f"{complete_count} per-problem values.json files complete"


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
        "reviewer_entry_map.md",
        "anchor_figure_plan.md",
        "entry_gate.md",
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
    manifest = project / "numbers_manifest.json"
    if manifest.is_file():
        numbers_ok, numbers_detail = run_python_check(root, ["scripts/verify_numbers.py", "--verify", str(project), base])
        ev.add("gate1_manifest_verify", numbers_ok, "verify_numbers --verify PASS" if numbers_ok else numbers_detail)
    else:
        ev.add("gate1_manifest_verify", False, "numbers_manifest.json missing")

    symbols_ok, symbols_detail = symbol_check_ok(root, project, base)
    ev.add("gate1_symbol_audit", symbols_ok, symbols_detail)

    step8_5_state = workflow_state.collect_step8_5_state(project)
    ev.add(
        "entry_gate_verdict",
        bool(step8_5_state.get("ready")),
        "entry_gate PASS" if step8_5_state.get("ready") else f"step8_5 status={step8_5_state.get('status')}",
    )

    verdict = workflow_state.gate2_verdict(project)
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
    canonical_ok, canonical_detail = canonical_results_state(project)
    ev.add("canonical_results", canonical_ok, canonical_detail)

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
