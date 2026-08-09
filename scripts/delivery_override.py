#!/usr/bin/env python3
"""Issue, inspect, and revoke administrator delivery overrides."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from web.backend.auth_store import AuthStore


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db", default=str(Path(__file__).resolve().parents[1] / "web/auth.db")
    )
    sub = parser.add_subparsers(dest="command", required=True)

    issue = sub.add_parser("issue")
    issue.add_argument("base_name")
    issue.add_argument(
        "--scope",
        required=True,
        choices=("continue_after_gate2", "deliver_snapshot"),
    )
    issue.add_argument("--snapshot")
    issue.add_argument("--source-verdict", required=True)
    issue.add_argument("--reason", required=True)
    issue.add_argument("--actor", default="admin")
    issue.add_argument("--expires-at", type=int)

    revoke = sub.add_parser("revoke")
    revoke.add_argument("override_id")
    revoke.add_argument("--actor", default="admin")

    listing = sub.add_parser("list")
    listing.add_argument("--base")

    args = parser.parse_args()
    store = AuthStore(Path(args.db))
    store.initialize()
    try:
        if args.command == "issue":
            record = store.issue_delivery_override(
                base_name=args.base_name,
                scope=args.scope,
                bound_snapshot_id=args.snapshot,
                source_verdict=args.source_verdict,
                reason=args.reason,
                actor=args.actor,
                expires_at=args.expires_at,
            )
            print(json.dumps(asdict(record), ensure_ascii=False, indent=2))
            return 0
        if args.command == "revoke":
            record = store.revoke_delivery_override(
                args.override_id, actor=args.actor
            )
            print(json.dumps(asdict(record), ensure_ascii=False, indent=2))
            return 0
        records = store.list_delivery_overrides(args.base)
        print(
            json.dumps(
                [asdict(record) for record in records],
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
