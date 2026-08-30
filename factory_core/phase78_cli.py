"""Explicit-enabled CLI adapter for the local Phase 7+8 shadow service."""

from __future__ import annotations

import argparse
import importlib
import json
from pathlib import Path
import stat
import sys


_MAX_REQUEST_BYTES = 8 * 1024 * 1024
_PUBLIC_CANCELLATION_REASONS = frozenset(
    {"user_cancel", "shutdown", "superseded"}
)


def _duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _payload(path_value: str) -> dict[str, object]:
    path = Path(path_value)
    information = path.lstat()
    if (
        not stat.S_ISREG(information.st_mode)
        or information.st_nlink != 1
        or not 0 < information.st_size <= _MAX_REQUEST_BYTES
    ):
        raise ValueError("Phase 7+8 request must be one bounded regular file")
    raw = path.read_bytes()
    if len(raw) != information.st_size:
        raise ValueError("Phase 7+8 request changed while reading")
    value = json.loads(
        raw.decode("utf-8", errors="strict"), object_pairs_hook=_duplicates
    )
    if type(value) is not dict:
        raise ValueError("Phase 7+8 request must be a JSON object")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python3 -m factory_core.cli phase78")
    parser.add_argument("--actor", default="local-operator")
    sub = parser.add_subparsers(dest="phase78_command", required=True)
    submit = sub.add_parser("submit")
    submit.add_argument("project_id")
    submit.add_argument("request_json")
    run = sub.add_parser("run-one")
    run.add_argument("project_id")
    run.add_argument("request_json")
    status = sub.add_parser("status")
    status.add_argument("project_id")
    status.add_argument("idempotency_key")
    cancel = sub.add_parser("cancel")
    cancel.add_argument("project_id")
    cancel.add_argument("request_json")
    revoke = sub.add_parser("revoke")
    revoke.add_argument("project_id")
    revoke.add_argument("approval_id")
    revoke.add_argument("request_json")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        config = importlib.import_module("factory_core.phase78_config")
        settings = config.load_phase78_settings()
        if not settings.enabled:
            print(
                json.dumps(
                    {
                        "code": "PHASE78_SHADOW_DISABLED",
                        "enabled": False,
                        "authoritative": False,
                        "authority_transferred": False,
                        "dispatch_performed": False,
                    },
                    sort_keys=True,
                ),
                file=sys.stderr,
            )
            return 64
        args = _parser().parse_args(arguments)
        service = importlib.import_module("factory_core.phase78_service")
        common = {
            "settings": settings,
            "project_id": args.project_id,
            "actor_id": args.actor,
        }
        if args.phase78_command == "submit":
            result = service.submit_phase78_request(
                **common, payload=_payload(args.request_json)
            )
        elif args.phase78_command == "run-one":
            result = service.run_phase78_worker_once(
                **common, payload=_payload(args.request_json)
            )
        elif args.phase78_command == "status":
            result = service.load_phase78_status(
                **common, idempotency_key=args.idempotency_key
            )
        elif args.phase78_command == "cancel":
            result = service.cancel_phase78_request(
                **common, payload=_payload(args.request_json)
            )
        else:
            result = service.revoke_phase78_approval(
                **common,
                approval_id=args.approval_id,
                payload=_payload(args.request_json),
            )
        value = result.as_dict() if callable(getattr(result, "as_dict", None)) else result
        print(json.dumps(value, ensure_ascii=False, sort_keys=True))
        return 0
    except SystemExit:
        raise
    except Exception as exc:
        code = getattr(exc, "code", "PHASE78_SHADOW_UNAVAILABLE")
        error = {"code": code}
        reason = getattr(exc, "reason", None)
        if reason in _PUBLIC_CANCELLATION_REASONS:
            error["reason"] = reason
        print(json.dumps(error, sort_keys=True), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
