#!/usr/bin/env python3
"""Shared delivery-contract helpers for Modeling Factory projects."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import evaluate_modeling_project


CURRENT_CONTRACT_VERSION = "2026-07-02.step8_5_gate2_zip"


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def sha256_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_commit(root: Path) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except Exception:
        return None
    commit = proc.stdout.strip()
    return commit if proc.returncode == 0 and commit else None


def check_map(ev: evaluate_modeling_project.Evaluation) -> dict[str, evaluate_modeling_project.Check]:
    return {check.name: check for check in ev.checks}


def classify_evaluation(ev: evaluate_modeling_project.Evaluation) -> str:
    checks = check_map(ev)
    if ev.passed and ev.inferred_step == 16:
        return "CURRENT_PASS"

    delivered_checks = ("papers_pdf", "submission_zip")
    if all(checks.get(name) and checks[name].ok for name in delivered_checks):
        return "LEGACY_DELIVERED"

    return "INVALID_OR_INCOMPLETE"


def artifact_record(path: Path) -> dict[str, Any]:
    return {
        "path": str(path),
        "exists": path.is_file(),
        "bytes": path.stat().st_size if path.is_file() else 0,
        "sha256": sha256_file(path),
    }


def build_delivery_manifest(
    project: Path,
    root: Path,
    ev: evaluate_modeling_project.Evaluation,
    *,
    generated_at: str | None = None,
) -> dict[str, Any]:
    project = project.resolve()
    root = root.resolve()
    base = project.name
    failed_checks = [check.__dict__ for check in ev.checks if not check.ok and check.severity != "warning"]

    return {
        "contract_version": CURRENT_CONTRACT_VERSION,
        "generated_at": generated_at or utc_now(),
        "status": classify_evaluation(ev),
        "runner_commit": git_commit(root),
        "project": {
            "base": base,
            "path": str(project),
        },
        "evaluation": {
            "inferred_step": ev.inferred_step,
            "passed": ev.passed,
            "failed_checks": failed_checks,
        },
        "artifacts": {
            "project_pdf": artifact_record(project / f"{base}_paper.pdf"),
            "papers_pdf": artifact_record(root / "papers" / f"{base}_paper.pdf"),
            "submission_zip": artifact_record(root / "papers" / f"{base}_submission.zip"),
        },
    }


def write_delivery_manifest(project: Path, root: Path, output: Path | None = None) -> dict[str, Any]:
    ev = evaluate_modeling_project.evaluate(project, root)
    manifest = build_delivery_manifest(project, root, ev)
    target = output or (project / "delivery_manifest.json")
    target.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", help="Path to complete/<base> or ongoing/<base> project directory.")
    parser.add_argument("--root", default=None, help="Factory root. Defaults to the repository root.")
    parser.add_argument("--output", default=None, help="Manifest path. Defaults to <project>/delivery_manifest.json.")
    args = parser.parse_args()

    root = Path(args.root).resolve() if args.root else evaluate_modeling_project.repo_root()
    project = Path(args.project).resolve()
    if not project.is_dir():
        print(f"Project directory not found: {project}", flush=True)
        return 2

    manifest = write_delivery_manifest(project, root, Path(args.output).resolve() if args.output else None)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0 if manifest["status"] == "CURRENT_PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
