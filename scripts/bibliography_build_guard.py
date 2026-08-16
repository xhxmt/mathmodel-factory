#!/usr/bin/env python3
"""Build or verify the content-addressed bibliography evidence receipt."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from factory_core.bibliography import (
    BIBLIOGRAPHY_RECEIPT_PATH,
    build_bibliography_receipt,
    verify_bibliography_receipt,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project")
    parser.add_argument("base")
    parser.add_argument("--backend")
    parser.add_argument("--backend-version", default="")
    parser.add_argument(
        "--backend-log", default="logs/compilation/bibliography_backend.log"
    )
    parser.add_argument("--final-log", default="logs/compilation/pass3.log")
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    project = Path(args.project).resolve()
    try:
        if args.verify:
            valid, errors, receipt = verify_bibliography_receipt(
                project, args.base
            )
            if not valid:
                raise ValueError("; ".join(errors))
            payload = receipt
        else:
            if args.backend is None:
                raise ValueError("--backend is required when building a receipt")
            payload = build_bibliography_receipt(
                project,
                args.base,
                backend=args.backend,
                backend_version=args.backend_version,
                backend_log=args.backend_log,
                final_log=args.final_log,
            )
        print(
            json.dumps(
                {
                    "status": "PASS",
                    "receipt": BIBLIOGRAPHY_RECEIPT_PATH.as_posix(),
                    "content_sha256": (payload or {}).get("content_sha256"),
                },
                sort_keys=True,
            )
        )
        return 0
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
