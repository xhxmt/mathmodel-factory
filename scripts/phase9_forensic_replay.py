#!/usr/bin/env python3
"""Explicit, default-off Phase9-A preflight/finalize/state commands."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from factory_core.canonical import canonical_bytes
from factory_core.phase9_config import load_phase9_settings
from factory_core.phase9_forensic_replay import (
    Phase9ForensicReplayError,
    Phase9ForensicReplayService,
    collect_phase9_forensic_replay_state,
    preflight_phase9_forensic_replay,
    read_phase9_forensic_replay_request,
)


def _emit(value: object) -> None:
    sys.stdout.buffer.write(canonical_bytes(value) + b"\n")


def _disabled() -> int:
    _emit(
        {
            "schema": "authority-phase9-forensic-command-v1",
            "status": "BLOCKED",
            "confirmed": False,
            "blockers": [
                {
                    "code": "PHASE9_DISABLED",
                    "detail": "PHASE9_ENABLED is false; no configured path was parsed",
                }
            ],
        }
    )
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    preflight = sub.add_parser("preflight", help="validate local evidence without DB writes")
    preflight.add_argument("--request", type=Path, required=True)
    preflight.add_argument("--evidence-root", type=Path, required=True)
    execute = sub.add_parser("execute", help="atomically finalize one authorized replay")
    execute.add_argument("--request", type=Path, required=True)
    execute.add_argument("--confirm", action="store_true")
    collect = sub.add_parser("collect", help="read current replay state query-only")
    collect.add_argument("--database", type=Path, required=True)
    collect.add_argument("--expected-source-fence", required=True)
    collect.add_argument("--workflow-id", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "preflight":
            request = read_phase9_forensic_replay_request(args.request)
            result = preflight_phase9_forensic_replay(
                request, evidence_root=args.evidence_root
            )
            _emit(result)
            return 0 if result["status"] == "READY" else 2
        if args.command == "collect":
            result = collect_phase9_forensic_replay_state(
                args.database,
                expected_source_fence_sha256=args.expected_source_fence,
                workflow_id=args.workflow_id,
            )
            _emit(result)
            return 0 if result["status"] == "COMPLETED" else 2

        settings = load_phase9_settings()
        if not settings.enabled:
            return _disabled()
        request = read_phase9_forensic_replay_request(args.request)
        assert settings.evidence_root is not None
        if not args.confirm:
            result = preflight_phase9_forensic_replay(
                request, evidence_root=settings.evidence_root
            )
            _emit(
                {
                    "schema": "authority-phase9-forensic-command-v1",
                    "status": result["status"],
                    "confirmed": False,
                    "preflight": result,
                }
            )
            return 0 if result["status"] == "READY" else 2
        assert settings.authority_database is not None
        assert settings.authority_source_fence_sha256 is not None
        assert settings.source_repository is not None
        result = Phase9ForensicReplayService(
            settings.authority_database,
            expected_source_fence_sha256=settings.authority_source_fence_sha256,
            source_repository=settings.source_repository,
            evidence_root=settings.evidence_root,
        ).execute(request)
        _emit(result.as_dict())
        return 0
    except (Phase9ForensicReplayError, OSError, ValueError) as exc:
        print(f"phase9_forensic_replay: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
