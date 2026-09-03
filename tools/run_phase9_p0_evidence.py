#!/usr/bin/env python3
"""Run the DB-backed fixed Phase9 P0 suite and emit formal evidence."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from factory_core.canonical import canonical_bytes
from factory_core.phase9_p0_evidence import (
    Phase9P0EvidenceError,
    produce_formal_phase9_p0_evidence,
)


def _absolute(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise argparse.ArgumentTypeError("path must be absolute")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", required=True, type=_absolute)
    parser.add_argument("--evidence-root", required=True, type=_absolute)
    parser.add_argument("--python", required=True, type=_absolute)
    parser.add_argument("--authority-db", required=True, type=_absolute)
    parser.add_argument("--expected-source-fence-sha256", required=True)
    parser.add_argument("--workflow-id", required=True)
    args = parser.parse_args(argv)
    try:
        bundle = produce_formal_phase9_p0_evidence(
            source_repository=args.source_root,
            evidence_root=args.evidence_root,
            python_executable=args.python,
            authority_database=args.authority_db,
            expected_source_fence_sha256=args.expected_source_fence_sha256,
            workflow_id=args.workflow_id,
        )
    except Phase9P0EvidenceError as exc:
        sys.stderr.write(f"BLOCKED: {exc}\n")
        return 2
    result = {
        "schema": "phase9-p0-formal-run-result-v2",
        "status": "PASS",
        "candidate": bundle.candidate,
        "coordinate": bundle.coordinate,
        "source_inventory_sha256": bundle.source_inventory_sha256,
        "p0_evidence_root": str(bundle.evidence_root),
        "p0_evidence_root_sha256": bundle.evidence_root_sha256,
        "authority_attestation_sha256": bundle.authority_attestation_sha256,
        "p0_receipts": {
            name: str(path) for name, path in sorted(bundle.receipt_paths.items())
        },
        "authorization_scope": {
            "phase9_a_forensic_replay": False,
            "provider_or_network": False,
            "production_outbox_or_delivery": False,
            "release": False,
            "deployment": False,
            "migration": False,
            "cutover": False,
        },
    }
    sys.stdout.buffer.write(canonical_bytes(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
