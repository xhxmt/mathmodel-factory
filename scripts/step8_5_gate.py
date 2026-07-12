#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


VERDICT_RE = re.compile(r"(?m)^VERDICT:\s*(PASS|REVISE)\s*$")
REQUIRED_FILES = (
    "reviewer_entry_map.md",
    "anchor_figure_plan.md",
    "entry_gate.md",
)
FRESHNESS_INPUTS = (
    "results/canonical_results.json",
    "visualization_log.md",
    "evaluation.md",
    "solve_log.md",
    "sensitivity_report.md",
    "model.md",
)


def collect_step8_5_state(project_dir: str | Path) -> dict:
    project = Path(project_dir)
    files = {name: project / name for name in REQUIRED_FILES}
    present = {name: path.is_file() and path.stat().st_size > 0 for name, path in files.items()}
    artifacts_complete = all(present.values())

    existing_inputs = [project / name for name in FRESHNESS_INPUTS if (project / name).is_file()]
    stale_inputs: list[str] = []
    if artifacts_complete and existing_inputs:
        oldest_output_mtime = min(path.stat().st_mtime_ns for path in files.values())
        stale_inputs = [
            str(path.relative_to(project))
            for path in existing_inputs
            if path.stat().st_mtime_ns > oldest_output_mtime
        ]
    fresh = artifacts_complete and not stale_inputs

    verdict = None
    if present["entry_gate.md"]:
        text = files["entry_gate.md"].read_text(encoding="utf-8", errors="replace")
        match = VERDICT_RE.search(text)
        verdict = match.group(1) if match else None

    if not artifacts_complete:
        status = "missing"
    elif verdict == "PASS" and not fresh:
        status = "stale"
    elif verdict == "PASS":
        status = "pass"
    elif verdict == "REVISE":
        status = "revise"
    else:
        status = "invalid"
    effective_verdict = "STALE" if status == "stale" else verdict

    return {
        "status": status,
        "verdict": verdict,
        "effective_verdict": effective_verdict,
        "ready": verdict == "PASS" and artifacts_complete and fresh,
        "artifacts_complete": artifacts_complete,
        "fresh": fresh,
        "stale_inputs": stale_inputs,
        "files": {name: str(path) for name, path in files.items()},
        "present": present,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("project_dir")
    parser.add_argument("--verdict", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    state = collect_step8_5_state(args.project_dir)
    if args.verdict:
        print(state["effective_verdict"] or "")
    elif args.json:
        print(json.dumps(state, ensure_ascii=False))
    else:
        print(state["status"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
