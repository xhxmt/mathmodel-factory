from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from pathlib import Path

from ..infrastructure.process import ProcessRequest, ProcessSupervisor


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--exit-file", required=True)
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--stdout", required=True)
    parser.add_argument("--stderr", required=True)
    parser.add_argument("--max-time", type=int, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        raise SystemExit("solver command is required")
    result = ProcessSupervisor().run(
        ProcessRequest(
            argv=command,
            cwd=Path(args.cwd),
            timeout_seconds=args.max_time,
            stdout_path=Path(args.stdout),
            stderr_path=Path(args.stderr),
            env=os.environ,
        )
    )
    status = (
        "completed"
        if result.returncode == 0
        else "timeout"
        if result.timed_out
        else "failed"
    )
    payload = {
        "status": status,
        "returncode": result.returncode,
        "finished_at": int(time.time()),
    }
    target = Path(args.exit_file)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
