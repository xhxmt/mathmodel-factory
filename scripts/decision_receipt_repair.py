#!/usr/bin/env python3
"""Deterministically restore one missing human-decision receipt from SQLite."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from factory_core.domain import InvalidTransition
from factory_core.storage import SQLiteStateStore


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project")
    parser.add_argument("request_id")
    args = parser.parse_args()
    try:
        result = SQLiteStateStore(Path(args.project).resolve()).repair_decision_receipt(
            args.request_id
        )
    except (OSError, InvalidTransition, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
