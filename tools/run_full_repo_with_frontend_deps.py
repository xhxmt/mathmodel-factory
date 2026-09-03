#!/usr/bin/env python3
"""Run full pytest with a runner-mounted, read-only frontend dependency tree."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess


def _verify_locked_dependencies(source: Path, dependency: Path) -> tuple[str, int, int]:
    lock_path = source / "web/frontend/package-lock.json"
    lock_bytes = lock_path.read_bytes()
    lock = json.loads(lock_bytes)
    packages = lock.get("packages")
    if type(packages) is not dict or "" not in packages:
        raise RuntimeError("frontend package-lock packages map is invalid")
    verified = 0
    optional_absent = 0
    for lock_key, expected in sorted(packages.items()):
        if not lock_key.startswith("node_modules/"):
            continue
        if type(expected) is not dict or type(expected.get("version")) is not str:
            raise RuntimeError(f"lock entry lacks an exact version: {lock_key}")
        relative = lock_key.removeprefix("node_modules/")
        package_file = dependency / relative / "package.json"
        if not package_file.is_file() or package_file.is_symlink():
            # The Python repository suite does not execute development-only or
            # platform-optional packages.  Their absence is recorded, while a
            # missing runtime package remains a hard failure.
            if expected.get("optional") is True or expected.get("dev") is True:
                optional_absent += 1
                continue
            raise RuntimeError(f"locked dependency is absent or unsafe: {lock_key}")
        installed = json.loads(package_file.read_bytes())
        if installed.get("version") != expected["version"]:
            raise RuntimeError(f"locked dependency version differs: {lock_key}")
        verified += 1
    if verified == 0:
        raise RuntimeError("no locked frontend dependency was verified")
    return hashlib.sha256(lock_bytes).hexdigest(), verified, optional_absent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--dependency-target", required=True, type=Path)
    parser.add_argument("--python", required=True, type=Path)
    parser.add_argument("--basetemp", required=True, type=Path)
    args = parser.parse_args(argv)

    source = args.source_root.resolve(strict=True)
    dependency = args.dependency_target.resolve(strict=True)
    python = args.python.absolute()
    python.resolve(strict=True)
    if not python.is_file() or not stat.S_ISDIR(dependency.lstat().st_mode):
        raise RuntimeError("Python or dependency target is not an ordinary entry")
    lock_sha256, locked_packages, optional_absent = _verify_locked_dependencies(
        source, dependency
    )
    print(f"frontend_lock_sha256={lock_sha256}", flush=True)
    print(f"locked_packages_verified={locked_packages}", flush=True)
    print(f"locked_nonruntime_packages_absent={optional_absent}", flush=True)

    link = source / "web/frontend/node_modules"
    if not link.is_symlink() or link.resolve(strict=True) != dependency:
        raise RuntimeError(
            "frontend dependency must be mounted by the trusted audit sandbox"
        )
    info = link.lstat()
    if not stat.S_ISLNK(info.st_mode):
        raise RuntimeError("frontend dependency sandbox mount differs")
    # The trusted parent already records a recursive byte inventory.  The
    # lock-entry walk above independently checks every installed version and
    # fails on any missing runtime entry.  Running npm here would reclassify
    # explicitly permitted absent development packages as fatal and would add
    # no stronger byte binding.
    print("frontend_dependency_inventory=PASS", flush=True)
    reporter = os.environ.get("PHASE9_TRUSTED_PYTEST_REPORTER_PATH")
    runtime_site_packages = os.environ.get("PHASE9_TRUSTED_PYTEST_SITE_PACKAGES")
    event_fd_text = os.environ.get("PHASE9_TRUSTED_PYTEST_EVENT_FD")
    if (
        not reporter
        or not runtime_site_packages
        or os.environ.get("PHASE9_TRUSTED_PYTEST_EVENT_PATH")
        != "PARENT_CAPTURED_ANONYMOUS_PIPE"
        or not event_fd_text
        or not event_fd_text.isdecimal()
        or int(event_fd_text) < 3
        or not os.environ.get("PHASE9_TRUSTED_PYTEST_NONCE")
    ):
        raise RuntimeError("trusted pytest reporter coordinate is absent")
    event_fd = int(event_fd_text)
    if not stat.S_ISFIFO(os.fstat(event_fd).st_mode):
        raise RuntimeError("trusted pytest parent event channel is not a pipe")
    completed = subprocess.run(
        [
            str(python), "-I", "-S", "-B", reporter,
            "--runtime-site-packages", runtime_site_packages,
            "--source-root", str(source), "--", "-q",
            "-p", "no:cacheprovider",
            "--noconftest", "-c", "/dev/null", "--rootdir", str(source),
            "-o", "addopts=", f"--basetemp={args.basetemp}", "tests",
        ],
        cwd=source,
        check=False,
        pass_fds=(event_fd,),
    )
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
