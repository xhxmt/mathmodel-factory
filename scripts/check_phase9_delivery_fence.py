#!/usr/bin/env python3
"""Fail closed unless the current Authority permits a delivery side effect."""

from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from factory_core.phase9_delivery_fence import (
    delivery_side_effect_commit_lease,
    require_delivery_side_effect_authority,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project")
    parser.add_argument("--workflow-id")
    parser.add_argument("--run-generation")
    parser.add_argument(
        "--exec",
        dest="command",
        nargs=argparse.REMAINDER,
        help="run a mutation command while holding the final Authority lease",
    )
    args = parser.parse_args()
    try:
        if args.command:
            with delivery_side_effect_commit_lease(
                Path(args.project),
                operation="delivery",
                workflow_id=args.workflow_id,
                run_generation=args.run_generation,
            ):
                return subprocess.run(args.command, check=False).returncode
        else:
            require_delivery_side_effect_authority(
                Path(args.project),
                operation="delivery",
                workflow_id=args.workflow_id,
                run_generation=args.run_generation,
            )
    except (OSError, ValueError) as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
