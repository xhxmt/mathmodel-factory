#!/usr/bin/env python3
"""Run an isolated Codex TUI judge when ``codex exec`` is unavailable."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import pty
import re
import select
import signal
import struct
import subprocess
import termios
import time
from pathlib import Path


ANSI_ESCAPE = re.compile(
    rb"(?:\x1b\][^\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~])"
)


def plain_terminal_text(raw: bytes) -> bytes:
    return re.sub(rb"\s+", b"", ANSI_ESCAPE.sub(b"", raw))


def valid_protocol(path: Path) -> bool:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        return bool(lines) and lines[0].startswith("VERDICT:") and isinstance(
            json.loads("\n".join(lines[1:])), dict
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False


def stop_process_group(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    for sig, wait_seconds in ((signal.SIGINT, 5), (signal.SIGTERM, 5), (signal.SIGKILL, 1)):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=wait_seconds)
            return
        except subprocess.TimeoutExpired:
            continue


def run(args: argparse.Namespace) -> int:
    workdir = args.workdir.resolve()
    prompt_file = args.prompt_file.resolve()
    output_file = args.output_file.resolve()
    log_file = args.log_file.resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "-C", str(workdir), "init", "-q"],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    command = [
        "codex",
        "--model",
        args.model,
        "-c",
        f'model_reasoning_effort="{args.effort}"',
        "--full-auto",
        "-C",
        str(workdir),
        (
            f"Read {prompt_file.name} completely and follow it exactly. "
            "Start now, do not ask questions, and finish only after writing the required verdict file."
        ),
    ]
    master_fd, slave_fd = pty.openpty()
    fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
    environment = os.environ.copy()
    environment["TERM"] = "dumb"
    environment.pop("COLORTERM", None)
    process = subprocess.Popen(
        command,
        cwd=workdir,
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        start_new_session=True,
        env=environment,
    )
    os.close(slave_fd)
    os.set_blocking(master_fd, False)
    deadline = time.monotonic() + args.timeout
    dumb_terminal_confirmed = False
    trust_confirmed = False
    recent = bytearray()
    success = False

    try:
        with log_file.open("ab") as log:
            log.write(b"\n--- CODEX TUI FALLBACK ---\n")
            while time.monotonic() < deadline:
                readable, _, _ = select.select([master_fd], [], [], 0.5)
                if readable:
                    try:
                        chunk = os.read(master_fd, 65536)
                    except BlockingIOError:
                        chunk = b""
                    except OSError:
                        chunk = b""
                    if chunk:
                        log.write(chunk)
                        log.flush()
                        recent.extend(chunk)
                        if len(recent) > 200_000:
                            del recent[:-100_000]
                        plain = plain_terminal_text(bytes(recent))
                        if (
                            not dumb_terminal_confirmed
                            and b"Continueanyway?[y/N]:" in plain
                        ):
                            os.write(master_fd, b"y\r")
                            dumb_terminal_confirmed = True
                        if not trust_confirmed and b"Doyoutrustthecontents" in plain:
                            os.write(master_fd, b"\r")
                            trust_confirmed = True
                if valid_protocol(output_file):
                    success = True
                    break
                if process.poll() is not None:
                    break
    finally:
        stop_process_group(process)
        os.close(master_fd)

    if success:
        print(f"Codex TUI judge wrote protocol-valid output: {output_file}")
        return 0
    print(f"Codex TUI judge did not produce protocol-valid output: {output_file}")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--prompt-file", type=Path, required=True)
    parser.add_argument("--output-file", type=Path, required=True)
    parser.add_argument("--log-file", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--effort", default="xhigh")
    parser.add_argument("--timeout", type=int, default=3600)
    return run(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
