#!/usr/bin/env python3
"""Run bounded Step13 components in an explicit isolated project copy."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from factory_core.phase9_runtime import Phase9RuntimeError, run_step13_components


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "review"))
    parser.add_argument("--project-copy", type=Path, required=True)
    parser.add_argument("--records", type=Path, required=True)
    parser.add_argument("--mode", choices=("NORMAL_STEP13", "FORENSIC_THREE_ROLE"),
                        default="FORENSIC_THREE_ROLE")
    parser.add_argument("--timeout-seconds", type=int, default=900)
    parser.add_argument("--total-timeout-seconds", type=int, default=3600)
    parser.add_argument("--model", default="gpt-6-astra")
    parser.add_argument("--effort", default="medium")
    args = parser.parse_args()
    try:
        result = run_step13_components(
            source=Path(__file__).resolve().parents[1], project=args.project_copy,
            records=args.records, mode=args.mode, timeout_seconds=args.timeout_seconds,
            total_timeout_seconds=args.total_timeout_seconds, model=args.model,
            effort=args.effort, prepare_only=args.command == "prepare",
        )
    except (Phase9RuntimeError, OSError) as exc:
        print(type(exc).__name__ + ": " + str(exc), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] in {"PREPARED", "COMPONENT_PASS"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
