#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from factory_core.problem_plan import (  # noqa: E402
    ProblemPlanError,
    load_problem_plan,
    problem_plan_fingerprint,
    validate_problem_plan,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate a problem-plan-v1 dependency DAG.")
    parser.add_argument("plan", type=Path, help="Path to problem/problem_plan.json")
    parser.add_argument("--json", action="store_true", help="Print a machine-readable validation summary")
    args = parser.parse_args()
    try:
        if args.plan.parent.name == "problem":
            plan = load_problem_plan(args.plan.parent.parent)
        else:
            raw = json.loads(args.plan.read_text(encoding="utf-8"))
            plan = validate_problem_plan(raw)
    except (OSError, json.JSONDecodeError, ProblemPlanError) as exc:
        print(f"INVALID: {exc}", file=sys.stderr)
        return 1
    summary = {
        "schema_version": plan["schema_version"],
        "nodes": len(plan["nodes"]),
        "edges": len(plan["edges"]),
        "topological_order": plan["topological_order"],
        "sha256": problem_plan_fingerprint(plan),
    }
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(
            f"OK: {summary['nodes']} nodes, {summary['edges']} edges, "
            f"sha256={summary['sha256']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
