#!/usr/bin/env python3
"""Standalone, explicit-path Phase-2 Authority production operator."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import stat
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from factory_core.authority_operations import (
    AUTHORITY_PRIMARY,
    CANARY,
    V1_ONLY,
    AuthorityHealthPolicy,
    AuthorityOperations,
    BackupEvidence,
    RestoreEvidence,
    create_authority_backup,
    evaluate_authority_health,
    preflight_authority_restore,
    restore_authority_backup,
    write_operator_evidence,
)
from factory_core.authority_operator_workflow import (
    run_authority_migrate_operation,
    run_authority_restore_operation,
)
from factory_core.authority_production_schema import (
    migrate_authority_production_foundation,
    production_preflight,
    production_schema_status,
)
from factory_core.authority_schema import migrate_authority_schema_v2
from factory_core.canonical import canonical_bytes, canonical_sha256


def _emit(value: object) -> None:
    sys.stdout.buffer.write(canonical_bytes(value) + b"\n")


def _read_evidence_object(path: str) -> dict[str, object]:
    value = Path(path)
    metadata = value.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ValueError("evidence input must be a non-symlink regular file")
    payload = json.loads(value.read_bytes())
    if type(payload) is not dict:
        raise ValueError("evidence input must contain one JSON object")
    return payload


def _backup_evidence(path: str | None) -> BackupEvidence | None:
    if path is None:
        return None
    payload = _read_evidence_object(path)
    if payload.get("schema") in {
        "authority-production-migrate-operation-v1",
        "authority-production-migrate-operation-v2",
    }:
        if payload.get("schema") == "authority-production-migrate-operation-v2":
            recorded = payload.get("evidence_sha256")
            body = dict(payload)
            body.pop("evidence_sha256", None)
            if recorded != canonical_sha256(body):
                raise ValueError("migration evidence hash differs")
        value = payload.get("backup")
        if type(value) is not dict:
            raise ValueError("migration evidence lacks backup evidence")
        expected = payload.get("backup_evidence_sha256")
        if expected != canonical_sha256(value):
            raise ValueError("migration backup evidence hash differs")
        payload = value
    if payload.pop("schema", None) != "authority-production-backup-evidence-v2":
        raise ValueError("unsupported backup evidence schema")
    return BackupEvidence(**payload)


def _restore_evidence(path: str | None) -> RestoreEvidence | None:
    if path is None:
        return None
    payload = _read_evidence_object(path)
    if payload.get("schema") == "authority-production-restore-operation-v2":
        recorded = payload.get("evidence_sha256")
        body = dict(payload)
        body.pop("evidence_sha256", None)
        if recorded != canonical_sha256(body):
            raise ValueError("restore operation evidence hash differs")
        nested = payload.get("restore")
        if type(nested) is not dict:
            raise ValueError("restore operation evidence lacks restore facts")
        payload = dict(nested)
    recorded_digest = payload.pop("evidence_sha256", None)
    if recorded_digest is not None and recorded_digest != canonical_sha256(payload):
        raise ValueError("restore evidence hash differs")
    if payload.pop("schema", None) != "authority-production-restore-evidence-v2":
        raise ValueError("unsupported restore evidence schema")
    return RestoreEvidence(**payload)


def _mutation_gate(args: argparse.Namespace, operation: str) -> bool:
    if args.confirm:
        return True
    _emit(
        {
            "schema": "authority-operator-dry-run-v1",
            "operation": operation,
            "database": str(Path(args.database)),
            "confirmed": False,
        }
    )
    return False


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Explicit-path Authority production-foundation administration; "
            "mutations are dry-run unless --confirm is supplied"
        )
    )
    sub = parser.add_subparsers(dest="command", required=True)

    preflight = sub.add_parser("preflight", help="read-only schema-v9/source-fence check")
    preflight.add_argument("--database", required=True)
    preflight.add_argument("--database-id", required=True)
    preflight.add_argument("--expected-source-fence")

    status = sub.add_parser("status", help="read-only production migration state")
    status.add_argument("--database", required=True)

    migrate = sub.add_parser("migrate", help="backup then append the production suffix")
    migrate.add_argument("--database", required=True)
    migrate.add_argument("--database-id", required=True)
    migrate.add_argument("--expected-source-fence", required=True)
    migrate.add_argument("--backup", required=True)
    migrate.add_argument("--evidence-output", required=True)
    migrate.add_argument("--owner-token", required=True)
    migrate.add_argument("--occurred-at", required=True, type=int)
    migrate.add_argument("--confirm", action="store_true")

    restore = sub.add_parser("restore", help="restore a verified explicit backup")
    restore.add_argument("--database", required=True)
    restore.add_argument("--database-id", required=True)
    restore.add_argument("--backup", required=True)
    restore.add_argument("--expected-source-fence", required=True)
    restore.add_argument("--expected-backup-sha256", required=True)
    restore.add_argument("--expected-switch-epoch", required=True, type=int)
    restore.add_argument("--occurred-at", required=True, type=int)
    restore.add_argument("--evidence-output", required=True)
    restore.add_argument("--confirm", action="store_true")

    writer = sub.add_parser("configure-writer", help="CAS writer handoff/disable")
    writer.add_argument("--database", required=True)
    writer.add_argument("--expected-source-fence", required=True)
    writer.add_argument("--writer-id")
    writer.add_argument("--enabled", action="store_true")
    writer.add_argument("--expected-writer-epoch", required=True, type=int)
    writer.add_argument("--expected-switch-epoch", required=True, type=int)
    writer.add_argument("--operator-subject", required=True)
    writer.add_argument("--reason", required=True)
    writer.add_argument("--occurred-at", required=True, type=int)
    writer.add_argument("--confirm", action="store_true")

    consumer = sub.add_parser("configure-consumer", help="CAS consumer handoff/disable")
    consumer.add_argument("--database", required=True)
    consumer.add_argument("--expected-source-fence", required=True)
    consumer.add_argument("--consumer-id")
    consumer.add_argument("--enabled", action="store_true")
    consumer.add_argument("--expected-consumer-epoch", required=True, type=int)
    consumer.add_argument("--expected-switch-epoch", required=True, type=int)
    consumer.add_argument("--operator-subject", required=True)
    consumer.add_argument("--reason", required=True)
    consumer.add_argument("--occurred-at", required=True, type=int)
    consumer.add_argument("--confirm", action="store_true")

    switch = sub.add_parser("switch", help="CAS evidence-only route state")
    switch.add_argument("--database", required=True)
    switch.add_argument("--expected-source-fence", required=True)
    switch.add_argument("--target-mode", choices=[V1_ONLY, CANARY, AUTHORITY_PRIMARY], required=True)
    switch.add_argument("--expected-switch-epoch", required=True, type=int)
    switch.add_argument("--operator-subject", required=True)
    switch.add_argument("--reason", required=True)
    switch.add_argument("--occurred-at", required=True, type=int)
    switch.add_argument("--confirm", action="store_true")

    run_generation = sub.add_parser(
        "run-generation",
        help="atomically create/rotate one default-off candidate-bound run generation",
    )
    run_generation.add_argument("--database", required=True)
    run_generation.add_argument("--expected-source-fence", required=True)
    run_generation.add_argument("--source-repository", required=True)
    run_generation.add_argument("--official-input-root", required=True)
    run_generation.add_argument("--execution-context-receipt", required=True)
    run_generation.add_argument("--request", required=True)
    run_generation.add_argument("--confirm", action="store_true")

    health = sub.add_parser("health", help="read-only explicit-policy alert evaluation")
    health.add_argument("--database", required=True)
    health.add_argument("--expected-source-fence", required=True)
    health.add_argument("--evaluated-at", required=True, type=int)
    health.add_argument("--max-backlog-depth", required=True, type=int)
    health.add_argument("--max-oldest-pending-age", required=True, type=int)
    health.add_argument("--max-in-flight", required=True, type=int)
    health.add_argument("--max-expired-claims", required=True, type=int)
    health.add_argument("--max-retry-per-thousand", required=True, type=int)
    health.add_argument("--max-dead-letter-count", required=True, type=int)
    health.add_argument("--max-backup-age", required=True, type=int)
    health.add_argument("--require-restore-evidence", action="store_true")
    health.add_argument("--backup-evidence")
    health.add_argument("--restore-evidence")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "preflight":
            value = production_preflight(
                args.database,
                database_id=args.database_id,
                expected_source_fence_sha256=args.expected_source_fence,
            )
            _emit({**value.as_dict(), "preflight_sha256": value.preflight_sha256})
            return 0
        if args.command == "status":
            _emit({"schema": "authority-production-status-v1", "state": production_schema_status(args.database)})
            return 0
        if args.command == "migrate":
            preflight = production_preflight(
                args.database,
                database_id=args.database_id,
                expected_source_fence_sha256=args.expected_source_fence,
            )
            if not _mutation_gate(args, "migrate"):
                return 0
            evidence = run_authority_migrate_operation(
                args.database,
                database_id=args.database_id,
                expected_source_fence_sha256=args.expected_source_fence,
                backup=args.backup,
                evidence_output=args.evidence_output,
                owner_token=args.owner_token,
                occurred_at=args.occurred_at,
            )
            _emit(evidence)
            return 0
        if args.command == "restore":
            if not args.confirm:
                preflight = preflight_authority_restore(
                    args.database,
                    args.backup,
                    database_id=args.database_id,
                    expected_current_source_fence_sha256=args.expected_source_fence,
                    expected_backup_sha256=args.expected_backup_sha256,
                    expected_switch_epoch=args.expected_switch_epoch,
                )
                _emit(
                    {
                        "schema": "authority-operator-dry-run-v1",
                        "operation": "restore",
                        "database": str(Path(args.database)),
                        "confirmed": False,
                        "preflight": preflight.as_dict(),
                        "preflight_sha256": preflight.preflight_sha256,
                    }
                )
                return 0
            payload = run_authority_restore_operation(
                args.database,
                args.backup,
                database_id=args.database_id,
                occurred_at=args.occurred_at,
                expected_current_source_fence_sha256=args.expected_source_fence,
                expected_backup_sha256=args.expected_backup_sha256,
                expected_switch_epoch=args.expected_switch_epoch,
                evidence_output=args.evidence_output,
            )
            _emit(payload)
            return 0
        if args.command in {"configure-writer", "configure-consumer", "switch"}:
            if not _mutation_gate(args, args.command):
                return 0
            operations = AuthorityOperations(
                args.database, expected_source_fence_sha256=args.expected_source_fence
            )
            if args.command == "configure-writer":
                result = operations.configure_writer(
                    new_writer_id=args.writer_id, enabled=args.enabled,
                    expected_writer_epoch=args.expected_writer_epoch,
                    expected_switch_epoch=args.expected_switch_epoch,
                    operator_subject=args.operator_subject, reason=args.reason,
                    occurred_at=args.occurred_at,
                )
            elif args.command == "configure-consumer":
                result = operations.configure_consumer(
                    new_consumer_id=args.consumer_id, enabled=args.enabled,
                    expected_consumer_epoch=args.expected_consumer_epoch,
                    expected_switch_epoch=args.expected_switch_epoch,
                    operator_subject=args.operator_subject, reason=args.reason,
                    occurred_at=args.occurred_at,
                )
            else:
                result = operations.switch_mode(
                    target_mode=args.target_mode,
                    expected_switch_epoch=args.expected_switch_epoch,
                    operator_subject=args.operator_subject, reason=args.reason,
                    occurred_at=args.occurred_at,
                )
            _emit(asdict(result))
            return 0
        if args.command == "run-generation":
            from factory_core.phase9_run_generation import (
                run_generation_request_from_dict,
            )

            request = run_generation_request_from_dict(
                _read_evidence_object(args.request)
            )
            if not args.confirm:
                _emit(
                    {
                        "schema": "authority-operator-dry-run-v1",
                        "operation": "run-generation",
                        "database": str(Path(args.database)),
                        "confirmed": False,
                        "request_sha256": request.request_sha256,
                        "derived_run_generation": request.derived_run_generation,
                    }
                )
                return 0
            result = AuthorityOperations(
                args.database,
                expected_source_fence_sha256=args.expected_source_fence,
            ).create_or_rotate_run_generation(
                request,
                source_repository=args.source_repository,
                official_input_root=args.official_input_root,
                execution_context_receipt_path=args.execution_context_receipt,
            )
            _emit(result.as_dict())
            return 0
        if args.command == "health":
            policy = AuthorityHealthPolicy(
                args.max_backlog_depth, args.max_oldest_pending_age,
                args.max_in_flight, args.max_expired_claims,
                args.max_retry_per_thousand, args.max_dead_letter_count,
                args.max_backup_age, args.require_restore_evidence,
            )
            report = evaluate_authority_health(
                args.database, expected_source_fence_sha256=args.expected_source_fence,
                policy=policy, evaluated_at=args.evaluated_at,
                backup_evidence=_backup_evidence(args.backup_evidence),
                restore_evidence=_restore_evidence(args.restore_evidence),
            )
            _emit({**report.as_dict(), "report_sha256": report.report_sha256})
            return 0 if report.healthy else 1
        parser.error("unsupported command")
    except Exception as exc:
        print(f"authority_operator: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
