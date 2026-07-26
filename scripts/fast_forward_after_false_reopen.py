#!/usr/bin/env python3
"""Validate a metadata-only rerun before reusing the prior Step-15 paper.

The false Step-16 reopen can rerun Steps 5/6 even though the reviewed paper is
already complete.  This helper permits a fast-forward only when every
scientific numeric value in the regenerated manifest matches the saved
pre-reopen manifest.  Runtime/provenance/seed metadata may differ.
"""

from __future__ import annotations

import json
import math
import subprocess
import sys
from pathlib import Path


IGNORED_KEY_PARTS = (
    "runtime",
    "random_seed",
    "submitted_at",
    "completed_at",
    "provenance.",
    ".seed",
)


def run(command: list[str], *, stdout: Path | None = None, stderr: Path | None = None) -> None:
    out_handle = stdout.open("w", encoding="utf-8") if stdout else subprocess.DEVNULL
    err_handle = stderr.open("w", encoding="utf-8") if stderr else subprocess.DEVNULL
    try:
        subprocess.run(command, check=True, stdout=out_handle, stderr=err_handle)
    finally:
        if stdout:
            out_handle.close()
        if stderr:
            err_handle.close()


def scientific_values(manifest: dict) -> dict[tuple[str, str], object]:
    values: dict[tuple[str, str], object] = {}
    for source, entries in manifest.get("sources", {}).items():
        for key, record in entries.items():
            lowered = key.lower()
            if any(part in lowered for part in IGNORED_KEY_PARTS):
                continue
            values[(source, key)] = record.get("value")
    return values


def equivalent(left: object, right: object) -> bool:
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return math.isclose(float(left), float(right), rel_tol=1e-10, abs_tol=1e-12)
    return left == right


def main() -> int:
    if len(sys.argv) != 4:
        print("usage: fast_forward_after_false_reopen.py PROJECT BASE BASELINE_MANIFEST", file=sys.stderr)
        return 2

    project = Path(sys.argv[1]).resolve()
    base = sys.argv[2]
    baseline_path = Path(sys.argv[3]).resolve()
    factory = Path(__file__).resolve().parents[1]
    current_path = project / "numbers_manifest.json"

    if not baseline_path.is_file():
        print(f"FAST_FORWARD=NO baseline missing: {baseline_path}")
        return 1

    run(["python3", str(factory / "scripts/verify_numbers.py"), "--generate", str(project)])
    baseline = scientific_values(json.loads(baseline_path.read_text(encoding="utf-8")))
    current = scientific_values(json.loads(current_path.read_text(encoding="utf-8")))

    missing = sorted(set(baseline) - set(current))
    added = sorted(set(current) - set(baseline))
    changed = sorted(key for key in set(baseline) & set(current) if not equivalent(baseline[key], current[key]))
    if missing or added or changed:
        print(
            f"FAST_FORWARD=NO scientific manifest differs: "
            f"missing={len(missing)} added={len(added)} changed={len(changed)}"
        )
        for label, keys in (("missing", missing), ("added", added), ("changed", changed)):
            for key in keys[:10]:
                print(f"  {label}: {key[0]}::{key[1]}")
        return 1

    run(
        ["python3", str(factory / "scripts/verify_numbers.py"), "--verify", str(project), base],
        stdout=project / "number_verification.latest.stdout",
        stderr=project / "number_verification.latest.stderr",
    )
    run(
        ["python3", str(factory / "scripts/verify_deliverables.py"), str(project), base],
        stdout=project / "deliverables_verification.latest.txt",
    )
    run(
        ["python3", str(factory / "scripts/verify_invariants.py"), str(project)],
        stdout=project / "invariants_verification.latest.txt",
    )
    run(
        ["python3", str(factory / "scripts/verify_spec_impl.py"), str(project)],
        stdout=project / "spec_impl_verification.latest.txt",
    )
    run(
        ["python3", str(factory / "scripts/verify_provenance.py"), str(project)],
        stdout=project / "provenance_verification.latest.txt",
    )
    run(
        [
            "python3",
            str(factory / "scripts/verify_quality_contract.py"),
            str(project),
            "--factory-root",
            str(factory),
            "--json-out",
            str(project / "quality_contract_verification.latest.json"),
            "--text-out",
            str(project / "quality_contract_verification.latest.txt"),
        ]
    )

    print(f"FAST_FORWARD=YES scientific_values={len(current)} gates=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
