#!/usr/bin/env python3
"""Run full pytest with one verified temporary frontend dependency symlink."""

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
            if expected.get("optional") is True:
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
    print(f"locked_optional_packages_absent={optional_absent}", flush=True)

    link = source / "web/frontend/node_modules"
    if link.exists() or link.is_symlink():
        raise RuntimeError("temporary frontend dependency link already exists")
    os.symlink(str(dependency), link, target_is_directory=True)
    try:
        info = link.lstat()
        if not stat.S_ISLNK(info.st_mode) or os.readlink(link) != str(dependency):
            raise RuntimeError("temporary frontend dependency link differs")
        npm = subprocess.run(
            ["npm", "ls", "--all", "--json", "--prefix", str(source / "web/frontend")],
            cwd=source, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, check=False,
        )
        npm_wire = json.loads(npm.stdout or "{}")
        if npm.returncode != 0 or npm_wire.get("problems"):
            raise RuntimeError("frontend dependency closure does not satisfy the lock")
        print("frontend_npm_ls_all=PASS", flush=True)
        completed = subprocess.run(
            [
                str(python), "-m", "pytest", "-p", "no:cacheprovider",
                f"--basetemp={args.basetemp}", "-q",
            ],
            cwd=source, check=False,
        )
        return completed.returncode
    finally:
        info = link.lstat()
        if not stat.S_ISLNK(info.st_mode) or os.readlink(link) != str(dependency):
            raise RuntimeError("refusing to unlink a changed dependency entry")
        link.unlink()
        if link.exists() or link.is_symlink() or not dependency.is_dir():
            raise RuntimeError("dependency-link cleanup failed")
        print("temporary_frontend_dependency_link=REMOVED_TARGET_PRESERVED", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
