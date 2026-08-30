#!/usr/bin/env python3
"""Strictly verify a full-shadow candidate ZIP and outer SHA256SUMS."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from archive_tools.archive_safety import (  # noqa: E402
    ArchivePolicyError,
    verify_archive,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", help="absolute or relative candidate ZIP path")
    parser.add_argument(
        "--outer-sha256sums",
        required=True,
        help="outer SHA256SUMS path (must cover exactly this archive)",
    )
    parser.add_argument("--max-member-bytes", type=int, default=256 * 1024 * 1024)
    parser.add_argument("--max-total-bytes", type=int, default=1024 * 1024 * 1024)
    parser.add_argument("--max-ratio", type=float, default=200.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        result = verify_archive(
            Path(args.archive),
            outer_checksums=Path(args.outer_sha256sums),
            max_member_bytes=args.max_member_bytes,
            max_total_bytes=args.max_total_bytes,
            max_ratio=args.max_ratio,
        )
    except (ArchivePolicyError, OSError, ValueError) as exc:
        print(f"VERIFY_REJECTED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
