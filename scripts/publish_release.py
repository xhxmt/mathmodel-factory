#!/usr/bin/env python3
"""Publish the already-approved final-audit snapshot as an atomic release."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from factory_core.delivery.release import ReleasePublisher


APPROVED_STATUSES = {"PASS", "OVERRIDDEN"}


def publish_current_audit(
    project: Path,
    root: Path,
    *,
    workflow_id: str | None = None,
    run_generation: str | None = None,
):
    project = project.resolve()
    root = root.resolve()
    from factory_core.phase9_delivery_fence import require_phase9_delivery_authority

    # The CLI performs its own Authority check before trusting any project-local
    # audit file or launching the package subprocess.  ReleasePublisher repeats
    # the check at its mutation boundary.
    delivery_fence = require_phase9_delivery_authority(
        project,
        workflow_id=workflow_id,
        run_generation=run_generation,
    )
    latest = project / ".factory" / "audits" / "latest.json"
    try:
        audit = json.loads(latest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"final audit record is missing or invalid: {exc}") from exc

    snapshot_id = audit.get("snapshot_id")
    status = audit.get("status")
    if (
        audit.get("profile") != "final"
        or status not in APPROVED_STATUSES
        or audit.get("delivery_allowed") is not True
        or not isinstance(snapshot_id, str)
        or len(snapshot_id) != 64
        or any(character not in "0123456789abcdef" for character in snapshot_id)
    ):
        raise ValueError("latest final audit does not authorize delivery")

    def build_package(output: Path) -> bool:
        result = subprocess.run(
            [
                sys.executable,
                str(root / "scripts" / "package_submission.py"),
                str(project),
                project.name,
                str(output),
                "--workflow-id",
                delivery_fence.workflow_id,
                "--run-generation",
                delivery_fence.run_generation,
            ],
            cwd=root,
            check=False,
        )
        return result.returncode == 0

    return ReleasePublisher(root / "papers").publish(
        project,
        snapshot_id,
        status=status,
        package_builder=build_package,
        workflow_id=delivery_fence.workflow_id,
        run_generation=delivery_fence.run_generation,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project")
    parser.add_argument("--root", required=True)
    parser.add_argument("--workflow-id", required=True)
    parser.add_argument("--run-generation", required=True)
    args = parser.parse_args()

    try:
        release = publish_current_audit(
            Path(args.project),
            Path(args.root),
            workflow_id=args.workflow_id,
            run_generation=args.run_generation,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "release_id": release.release_id,
                "release_manifest": str(release.manifest),
                "release_pointer": str(release.pointer),
                "release_reused": release.reused,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
