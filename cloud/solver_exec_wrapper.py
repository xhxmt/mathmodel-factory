#!/usr/bin/env python3
"""Apply hard resource limits, then replace this process with a solver script."""

from __future__ import annotations

import argparse
import os
import resource
import sys


def apply_resource_limits(
    max_time: int,
    address_space_bytes: int,
    file_size_bytes: int,
    open_files: int,
    processes: int,
) -> None:
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_CPU, (max_time + 5, max_time + 5))
    resource.setrlimit(
        resource.RLIMIT_AS,
        (address_space_bytes, address_space_bytes),
    )
    resource.setrlimit(
        resource.RLIMIT_FSIZE,
        (file_size_bytes, file_size_bytes),
    )
    resource.setrlimit(resource.RLIMIT_NOFILE, (open_files, open_files))
    if hasattr(resource, "RLIMIT_NPROC"):
        resource.setrlimit(resource.RLIMIT_NPROC, (processes, processes))


def main() -> None:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--max-time", required=True, type=int)
    parser.add_argument("--address-space-bytes", required=True, type=int)
    parser.add_argument("--file-size-bytes", required=True, type=int)
    parser.add_argument("--open-files", required=True, type=int)
    parser.add_argument("--processes", required=True, type=int)
    parser.add_argument("script")
    args = parser.parse_args()
    if not 1 <= args.max_time <= 3600:
        raise SystemExit("invalid execution time")

    numeric_limits = (
        args.address_space_bytes,
        args.file_size_bytes,
        args.open_files,
        args.processes,
    )
    if any(value <= 0 for value in numeric_limits):
        raise SystemExit("invalid resource limit")
    apply_resource_limits(args.max_time, *numeric_limits)
    os.execve(sys.executable, [sys.executable, args.script], dict(os.environ))


if __name__ == "__main__":
    main()
