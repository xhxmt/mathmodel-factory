#!/usr/bin/env python3
"""Audit complete/ projects against the current delivery contract."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import delivery_contract, evaluate_modeling_project


STATUSES = (
    "CURRENT_PASS",
    "GATE2_OVERRIDE_DELIVERED",
    "LEGACY_DELIVERED",
    "INVALID_OR_INCOMPLETE",
)


def project_entry(project: Path, root: Path, *, write_manifest: bool = False) -> dict[str, Any]:
    ev = evaluate_modeling_project.evaluate(project, root)
    status = delivery_contract.classify_evaluation(ev, project)
    failed = [check.__dict__ for check in ev.checks if not check.ok and check.severity != "warning"]
    manifest = project / "delivery_manifest.json"
    if write_manifest:
        data = delivery_contract.build_delivery_manifest(project, root, ev)
        manifest.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {
        "base": project.name,
        "path": str(project),
        "status": status,
        "contract_version": delivery_contract.CURRENT_CONTRACT_VERSION,
        "inferred_step": ev.inferred_step,
        "passed": ev.passed,
        "failed_checks": failed,
        "delivery_manifest_exists": manifest.is_file(),
    }


def audit_complete_projects(complete_dir: Path, root: Path, *, write_manifests: bool = False) -> dict[str, Any]:
    complete_dir = complete_dir.resolve()
    root = root.resolve()
    projects = [
        project_entry(path, root, write_manifest=write_manifests)
        for path in sorted(complete_dir.iterdir())
        if path.is_dir() and not path.name.startswith(".")
    ]
    counts = Counter(entry["status"] for entry in projects)
    return {
        "contract_version": delivery_contract.CURRENT_CONTRACT_VERSION,
        "complete_dir": str(complete_dir),
        "summary": {status: counts.get(status, 0) for status in STATUSES},
        "projects": projects,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "complete_dir",
        nargs="?",
        default=None,
        help="Path to complete/. Defaults to <repo>/complete.",
    )
    parser.add_argument("--root", default=None, help="Factory root. Defaults to the repository root.")
    parser.add_argument(
        "--output",
        default=None,
        help="Output JSON path. Defaults to <complete_dir>/_validation_index.json.",
    )
    parser.add_argument("--no-write", action="store_true", help="Print JSON without writing the index file.")
    parser.add_argument(
        "--write-manifests",
        action="store_true",
        help="Write delivery_manifest.json into each audited project.",
    )
    args = parser.parse_args()

    root = Path(args.root).resolve() if args.root else evaluate_modeling_project.repo_root()
    complete_dir = Path(args.complete_dir).resolve() if args.complete_dir else root / "complete"
    if not complete_dir.is_dir():
        print(f"Complete directory not found: {complete_dir}", flush=True)
        return 2

    result = audit_complete_projects(complete_dir, root, write_manifests=args.write_manifests)
    text = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if not args.no_write:
        output = Path(args.output).resolve() if args.output else complete_dir / "_validation_index.json"
        output.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0 if result["summary"]["INVALID_OR_INCOMPLETE"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
