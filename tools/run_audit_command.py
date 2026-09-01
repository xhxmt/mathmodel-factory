#!/usr/bin/env python3
"""Run one audit command, preserve its raw merged log, and record exact outcomes."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from factory_core.canonical import canonical_bytes


_OUTCOMES = ("passed", "failed", "errors", "skipped", "xfailed", "xpassed")


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _last(pattern: str, text: str) -> int:
    values = re.findall(pattern, text, flags=re.IGNORECASE | re.MULTILINE)
    return int(values[-1]) if values else 0


def _stats(text: str, kind: str) -> dict[str, int]:
    if kind == "bootstrap":
        result = {
            name: _last(rf"^{name}=(\d+)\s*$", text)
            for name in _OUTCOMES
        }
    else:
        patterns = {
            "passed": r"(\d+)\s+passed\b",
            "failed": r"(\d+)\s+failed\b",
            "errors": r"(\d+)\s+errors?\b",
            "skipped": r"(\d+)\s+skipped\b",
            "xfailed": r"(\d+)\s+xfailed\b",
            "xpassed": r"(\d+)\s+xpassed\b",
        }
        result = {name: _last(pattern, text) for name, pattern in patterns.items()}
    result["warnings"] = _last(r"(\d+)\s+warnings?\b", text)
    result["collected"] = sum(result[name] for name in _OUTCOMES)
    return result


def _git(repository: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repository, check=True, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True,
    ).stdout.strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--id", required=True)
    parser.add_argument("--environment", required=True, choices=("source", "fresh"))
    parser.add_argument("--kind", required=True, choices=("pytest", "bootstrap"))
    parser.add_argument("--cwd", required=True, type=Path)
    parser.add_argument("--log", required=True, type=Path)
    parser.add_argument("--record", required=True, type=Path)
    parser.add_argument("--candidate-commit")
    parser.add_argument("--candidate-tree")
    parser.add_argument("--candidate-parent")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = list(args.command)
    if command and command[0] == "--":
        command.pop(0)
    if not command:
        parser.error("a command is required after --")
    cwd = args.cwd.resolve(strict=True)
    args.log.parent.mkdir(parents=True, exist_ok=True)
    args.record.parent.mkdir(parents=True, exist_ok=True)
    if args.environment == "source":
        commit = _git(cwd, "rev-parse", "HEAD")
        tree = _git(cwd, "rev-parse", "HEAD^{tree}")
        parents = _git(cwd, "show", "-s", "--format=%P", "HEAD").split()
        if len(parents) != 1:
            raise RuntimeError("audit candidate must have exactly one parent")
        parent = parents[0]
    else:
        commit, tree, parent = (
            args.candidate_commit, args.candidate_tree, args.candidate_parent
        )
        if not all((commit, tree, parent)):
            parser.error("fresh commands require candidate commit/tree/parent")
    started = _utc()
    before = time.monotonic()
    environment = dict(os.environ)
    environment.update(
        {
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONHASHSEED": "0",
            "PYTHONNOUSERSITE": "1",
        }
    )
    environment.pop("PHASE9_ENABLED", None)
    environment.pop("PHASE78_ENABLED", None)
    chunks: list[bytes] = []
    with args.log.open("wb") as output:
        process = subprocess.Popen(
            command, cwd=cwd, env=environment, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        assert process.stdout is not None
        while True:
            chunk = process.stdout.read1(65536)
            if not chunk:
                break
            output.write(chunk)
            output.flush()
            chunks.append(chunk)
            sys.stdout.buffer.write(chunk)
            sys.stdout.buffer.flush()
        exit_code = process.wait()
    duration = time.monotonic() - before
    raw = b"".join(chunks)
    text = raw.decode("utf-8", errors="replace")
    stats = _stats(text, args.kind)
    record = {
        "schema": "paper-factory-phase9-audit-command-v1",
        "id": args.id,
        "environment": args.environment,
        "kind": args.kind,
        "command_argv": command,
        "command_shell": subprocess.list2cmdline(command),
        "cwd": str(cwd),
        "candidate": {"commit": commit, "tree": tree, "parent": parent},
        "start_utc": started,
        "end_utc": _utc(),
        "duration_seconds_millis": round(duration * 1000),
        "exit_code": exit_code,
        "outcomes": stats,
        "log": str(args.log.resolve()),
        "log_bytes": len(raw),
        "log_sha256": hashlib.sha256(raw).hexdigest(),
        "python_version": sys.version,
    }
    args.record.write_bytes(canonical_bytes(record) + b"\n")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
