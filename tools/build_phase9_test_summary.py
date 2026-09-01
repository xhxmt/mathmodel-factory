#!/usr/bin/env python3
"""Verify Phase9 command/log records and build the exact paired test summary."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from factory_core.canonical import canonical_bytes, canonical_sha256


OUTCOMES = ("collected", "passed", "failed", "errors", "skipped", "xfailed", "xpassed", "warnings")


def _read(path: Path) -> dict[str, object]:
    raw = path.read_bytes()
    value = json.loads(raw)
    if type(value) is not dict or canonical_bytes(value) + b"\n" != raw:
        raise RuntimeError(f"command record is not canonical: {path}")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    records = []
    candidate = None
    for path in sorted(args.records.glob("*.json")):
        value = _read(path)
        log = Path(str(value["log"]))
        raw_log = log.read_bytes()
        if (
            len(raw_log) != value["log_bytes"]
            or hashlib.sha256(raw_log).hexdigest() != value["log_sha256"]
        ):
            raise RuntimeError(f"raw log binding differs: {path.name}")
        if candidate is None:
            candidate = value["candidate"]
        elif value["candidate"] != candidate:
            raise RuntimeError("command records bind different candidates")
        record = {
            "id": value["id"],
            "environment": value["environment"],
            "command_record_sha256": canonical_sha256(value),
            "command_argv": value["command_argv"],
            "exit_code": value["exit_code"],
            "outcomes": value["outcomes"],
            "log_bytes": value["log_bytes"],
            "log_sha256": value["log_sha256"],
        }
        records.append(record)
    if not records:
        raise RuntimeError("no command records found")
    by_suite: dict[str, dict[str, dict[str, object]]] = {}
    for record in records:
        identifier = str(record["id"])
        try:
            environment, suite = identifier.split("_", 1)
        except ValueError as exc:
            raise RuntimeError(f"record id lacks environment prefix: {identifier}") from exc
        if environment != record["environment"]:
            raise RuntimeError(f"record environment differs: {identifier}")
        by_suite.setdefault(suite, {})[environment] = record
    pairs = []
    exact = True
    for suite, environments in sorted(by_suite.items()):
        if set(environments) != {"source", "fresh"}:
            raise RuntimeError(f"suite is not source/fresh paired: {suite}")
        source = environments["source"]
        fresh = environments["fresh"]
        source_outcomes = source["outcomes"]
        fresh_outcomes = fresh["outcomes"]
        equal = source_outcomes == fresh_outcomes
        exact = exact and equal
        pairs.append(
            {
                "suite": suite,
                "source_id": source["id"],
                "fresh_id": fresh["id"],
                "source_outcomes": source_outcomes,
                "fresh_outcomes": fresh_outcomes,
                "exact_outcome_match": equal,
            }
        )
    non_pass = {name: 0 for name in ("failed", "errors", "skipped", "xfailed", "xpassed")}
    for record in records:
        for name in non_pass:
            non_pass[name] += int(record["outcomes"][name])
    result = "PASS" if (
        exact
        and all(record["exit_code"] == 0 for record in records)
        and all(value == 0 for value in non_pass.values())
    ) else "NONPASS"
    body = {
        "schema": "paper-factory-phase9-final-test-summary-v1",
        "candidate": candidate,
        "result": result,
        "source_fresh_exact": exact,
        "non_pass_totals": non_pass,
        "pairs": pairs,
        "records": records,
    }
    body["summary_sha256"] = canonical_sha256(body)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(canonical_bytes(body) + b"\n")
    print(canonical_bytes(body).decode())
    return 0 if result == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
