#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path


VERSION = 1
STATUS_FILE = "status.json"
EVENTS_FILE = "events.jsonl"


def diagnostics_dir(project_dir: str | Path) -> Path:
    return Path(project_dir) / "diagnostics"


def _status_path(project_dir: str | Path) -> Path:
    return diagnostics_dir(project_dir) / STATUS_FILE


def _events_path(project_dir: str | Path) -> Path:
    return diagnostics_dir(project_dir) / EVENTS_FILE


def _ensure_dir(project_dir: str | Path) -> Path:
    path = diagnostics_dir(project_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _atomic_write_json(path: Path, payload: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def write_status(
    project_dir: str | Path,
    *,
    state: str,
    current_step: int,
    current_action: str,
    reason_code: str = "",
    reason_summary: str = "",
    suggested_actions: list[str] | None = None,
    evidence: list[dict] | None = None,
    since: int | None = None,
    last_event_at: int | None = None,
) -> dict:
    _ensure_dir(project_dir)
    now = int(time.time())
    payload = {
        "version": VERSION,
        "state": state,
        "current_step": current_step,
        "current_action": current_action,
        "reason_code": reason_code,
        "reason_summary": reason_summary,
        "since": since or now,
        "last_event_at": last_event_at or now,
        "suggested_actions": suggested_actions or [],
        "evidence": evidence or [],
    }
    _atomic_write_json(_status_path(project_dir), payload)
    return payload


def load_status(project_dir: str | Path) -> dict | None:
    path = _status_path(project_dir)
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def append_event(
    project_dir: str | Path,
    *,
    step: int,
    event_type: str,
    message: str,
    reason_code: str = "",
    files: list[str] | None = None,
    meta: dict | None = None,
) -> dict:
    _ensure_dir(project_dir)
    event = {
        "ts": int(time.time()),
        "step": step,
        "type": event_type,
        "message": message,
        "reason_code": reason_code,
        "files": files or [],
        "meta": meta or {},
    }
    with _events_path(project_dir).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False) + "\n")
    return event


def load_recent_events(project_dir: str | Path, limit: int = 5) -> list[dict]:
    path = _events_path(project_dir)
    if not path.is_file():
        return []
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    rows = []
    for idx, line in enumerate(lines):
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            if idx == len(lines) - 1:
                break
            raise
    return rows[-limit:]


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)

    status_cmd = sub.add_parser("write-status")
    status_cmd.add_argument("project_dir")
    status_cmd.add_argument("--state", required=True)
    status_cmd.add_argument("--step", required=True, type=int)
    status_cmd.add_argument("--action", required=True)
    status_cmd.add_argument("--reason-code", default="")
    status_cmd.add_argument("--reason-summary", default="")
    status_cmd.add_argument("--since", type=int, default=None)
    status_cmd.add_argument("--suggested-action", action="append", default=[])
    status_cmd.add_argument("--evidence", action="append", default=[])

    event_cmd = sub.add_parser("append-event")
    event_cmd.add_argument("project_dir")
    event_cmd.add_argument("--step", required=True, type=int)
    event_cmd.add_argument("--type", required=True, dest="event_type")
    event_cmd.add_argument("--message", required=True)
    event_cmd.add_argument("--reason-code", default="")
    event_cmd.add_argument("--file", action="append", default=[])

    args = parser.parse_args()
    if args.cmd == "write-status":
        evidence = []
        for item in args.evidence:
            kind, value = item.split(":", 1)
            if kind == "file":
                evidence.append({"kind": kind, "path": value})
            else:
                evidence.append({"kind": kind, "value": value})
        payload = write_status(
            args.project_dir,
            state=args.state,
            current_step=args.step,
            current_action=args.action,
            reason_code=args.reason_code,
            reason_summary=args.reason_summary,
            suggested_actions=args.suggested_action,
            evidence=evidence,
            since=args.since,
        )
        print(json.dumps(payload, ensure_ascii=False))
        return 0

    payload = append_event(
        args.project_dir,
        step=args.step,
        event_type=args.event_type,
        message=args.message,
        reason_code=args.reason_code,
        files=args.file,
    )
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
