#!/usr/bin/env python3
"""Read-only Phase9 candidate entry state collector and gate verifier."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time

from factory_core.canonical import canonical_bytes, canonical_sha256
from factory_core.phase9_entry import (
    P0_REQUIREMENTS,
    CandidateIdentity,
    Phase9EntryError,
    blocked_phase9_entry_result,
    candidate_identity_from_dict,
    collect_phase9_entry_state,
    read_canonical_json_file,
    verify_candidate_source,
    verify_phase9_entry_gate,
)


REQUEST_SCHEMA = "phase9-entry-gate-request-v2"
COLLECT_SCHEMA = "phase9-entry-state-collection-request-v1"


def _request(path: Path, *, schema: str, fields: set[str]) -> dict[str, object]:
    value = read_canonical_json_file(path, label="Phase9 entry request")
    if set(value) != fields or value.get("schema") != schema:
        raise Phase9EntryError("Phase9 entry request schema/keys differ")
    return value


def _path(value: object, name: str, *, nullable: bool = False) -> Path | None:
    if nullable and value is None:
        return None
    if type(value) is not str or not value or not Path(value).is_absolute():
        raise Phase9EntryError(f"{name} must be an explicit absolute path")
    return Path(value)


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value:
        raise Phase9EntryError(f"{name} must be a non-empty string")
    return value


def _evaluated_at(value: object) -> int:
    if type(value) is not int or value < 0:
        raise Phase9EntryError("evaluated_at must be a nonnegative integer")
    return value


def _emit(value: object) -> None:
    sys.stdout.buffer.write(canonical_bytes(value))


def _collect(request_path: Path) -> int:
    value = _request(
        request_path,
        schema=COLLECT_SCHEMA,
        fields={"schema", "candidate", "authority_database", "workflow_id"},
    )
    candidate = candidate_identity_from_dict(value["candidate"])
    try:
        state = collect_phase9_entry_state(
            _path(value["authority_database"], "authority_database"),
            workflow_id=_text(value["workflow_id"], "workflow_id"),
            candidate=candidate,
        )
    except Exception as exc:
        now = int(time.time())
        _emit(
            blocked_phase9_entry_result(
                candidate=candidate,
                evaluated_at=now,
                trusted_now=now,
                error=exc,
            )
        )
        return 2
    _emit(state.as_dict())
    return 0


def _verify(request_path: Path) -> int:
    fields = {
        "schema",
        "evaluated_at",
        "candidate",
        "authority_database",
        "workflow_id",
        "source_root",
        "source_inventory",
        "p0_evidence_root",
        "p0_evidence_root_sha256",
        "official_input_root",
        "official_input_manifest",
        "execution_context",
        "operator_authorization",
        "p0_receipts",
    }
    value = _request(request_path, schema=REQUEST_SCHEMA, fields=fields)
    candidate = candidate_identity_from_dict(value["candidate"])
    now = _evaluated_at(value["evaluated_at"])
    trusted_now = int(time.time())
    source_sha256: str | None = None
    p0_hashes: dict[str, str] | None = None
    try:
        source = verify_candidate_source(
            _path(value["source_root"], "source_root"),
            candidate=candidate,
            inventory=_path(
                value["source_inventory"], "source_inventory", nullable=True
            ),
        )
        p0_files = value["p0_receipts"]
        if type(p0_files) is not dict or set(p0_files) != set(P0_REQUIREMENTS):
            raise Phase9EntryError("p0_receipts must name exactly all nine receipts")
        p0 = {
            name: read_canonical_json_file(
                _path(p0_files[name], f"p0_receipts.{name}"),
                label=f"{name} P0 receipt",
            )
            for name in P0_REQUIREMENTS
        }
        source_sha256 = canonical_sha256(source)
        state = collect_phase9_entry_state(
            _path(value["authority_database"], "authority_database"),
            workflow_id=_text(value["workflow_id"], "workflow_id"),
            candidate=candidate,
        )
        result = verify_phase9_entry_gate(
            state=state,
            source_verification=source,
            p0_receipts=p0,
            p0_evidence_root=_path(
                value["p0_evidence_root"], "p0_evidence_root"
            ),
            p0_evidence_root_sha256=_text(
                value["p0_evidence_root_sha256"], "p0_evidence_root_sha256"
            ),
            operator_authorization=read_canonical_json_file(
                _path(value["operator_authorization"], "operator_authorization"),
                label="operator authorization",
            ),
            official_input_manifest=read_canonical_json_file(
                _path(value["official_input_manifest"], "official_input_manifest"),
                label="official input manifest",
            ),
            official_input_root=_path(
                value["official_input_root"], "official_input_root"
            ),
            execution_context=read_canonical_json_file(
                _path(value["execution_context"], "execution_context"),
                label="execution context",
            ),
            evaluated_at=now,
            trusted_now=trusted_now,
        )
    except Exception as exc:
        result = blocked_phase9_entry_result(
            candidate=candidate,
            evaluated_at=now,
            trusted_now=trusted_now,
            error=exc,
            source_verification_sha256=source_sha256,
            p0_evidence_root_sha256=(
                value.get("p0_evidence_root_sha256")
                if type(value.get("p0_evidence_root_sha256")) is str
                and len(value["p0_evidence_root_sha256"]) == 64
                and set(value["p0_evidence_root_sha256"]) <= set("0123456789abcdef")
                else None
            ),
            p0_receipt_sha256s=p0_hashes,
        )
    _emit(result)
    return 0 if result["status"] == "READY" else 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("collect", "verify"):
        child = subparsers.add_parser(name)
        child.add_argument("--request", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "collect":
            return _collect(args.request)
        return _verify(args.request)
    except Phase9EntryError as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
