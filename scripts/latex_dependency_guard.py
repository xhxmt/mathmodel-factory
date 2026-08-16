#!/usr/bin/env python3
"""Fail closed when declared LaTeX inputs differ from compiler observations."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
import sys

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from factory_core.paper_sources import (
    LatexDependencyError,
    require_safe_latex_dependencies,
    verify_latex_recorder_inputs,
)


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project")
    parser.add_argument("base")
    parser.add_argument("--fls", action="append", default=[])
    parser.add_argument("--allowed-runtime-root", action="append", default=[])
    parser.add_argument("--output")
    parser.add_argument("--contract-lines", action="store_true")
    args = parser.parse_args()

    project = Path(args.project).resolve()
    try:
        graph = require_safe_latex_dependencies(project, args.base)
        if args.contract_lines:
            contract = graph.contract
            assert contract.root_source is not None
            print(contract.root_source.relative_to(project).as_posix())
            print(contract.engine)
            print(contract.job_name)
            print(contract.bibliography_backend)
            for search_root in contract.search_roots:
                relative = search_root.relative_to(project).as_posix()
                print(relative or ".")
            return 0
        payload: dict[str, object]
        if args.fls:
            payload = verify_latex_recorder_inputs(
                project,
                args.base,
                [Path(path) for path in args.fls],
                allowed_runtime_roots=args.allowed_runtime_root,
            )
        else:
            payload = {
                "status": "PASS",
                "dependency_graph": graph.manifest(),
            }
        if args.output:
            _write_json(Path(args.output), payload)
        else:
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    except (LatexDependencyError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
