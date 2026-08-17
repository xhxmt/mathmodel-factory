from __future__ import annotations

import json
import time
from collections import defaultdict
from typing import Any

from .workflow_events import canonical_hash

DIRTY_REBASE_SCHEMA = "factory-dirty-classifier-rebase-v1"


def ensure_dirty_rebase_schema(connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS dirty_classifier_rebases (
            rebase_id TEXT PRIMARY KEY,
            source_schema_version INTEGER NOT NULL,
            target_schema_version INTEGER NOT NULL,
            old_classifier_sha256 TEXT NOT NULL,
            new_classifier_sha256 TEXT NOT NULL,
            obligation_count INTEGER NOT NULL,
            created_at INTEGER NOT NULL,
            receipt_json TEXT NOT NULL
        );
        CREATE TRIGGER IF NOT EXISTS dirty_classifier_rebases_append_only_update
        BEFORE UPDATE ON dirty_classifier_rebases
        BEGIN
            SELECT RAISE(ABORT, 'dirty classifier rebases are append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS dirty_classifier_rebases_append_only_delete
        BEFORE DELETE ON dirty_classifier_rebases
        BEGIN
            SELECT RAISE(ABORT, 'dirty classifier rebases are append-only');
        END;
        """
    )


def _table_exists(connection, name: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def rebase_dirty_classifier_state(
    connection,
    *,
    source_schema_version: int,
    target_schema_version: int,
) -> dict[str, Any] | None:
    """Rebase mutable active obligations while preserving append-only history.

    Unresolved causes are reconstructed per ``(flag, owner_stage)`` using the
    latest cause that has no later clear receipt.  Existing active rows are
    retained conservatively when historical reconstruction is incomplete.
    Only the mutable active index receives the current classifier identity;
    causes and historical clear receipts remain byte-for-byte historical.
    """

    ensure_dirty_rebase_schema(connection)
    if not _table_exists(connection, "dirty_flags"):
        return None
    from .dirty import classifier_contract_sha256

    current_classifier = classifier_contract_sha256()
    active_rows = {
        (str(row["flag"]), int(row["owner_stage"])): dict(row)
        for row in connection.execute(
            "SELECT * FROM dirty_flags ORDER BY flag, owner_stage"
        ).fetchall()
    }
    reconstructed: dict[tuple[str, int], dict[str, Any]] = {}
    if _table_exists(connection, "dirty_causes"):
        clear_revisions: dict[tuple[str, int], list[int]] = defaultdict(list)
        if _table_exists(connection, "dirty_flag_clear_receipts"):
            for row in connection.execute(
                "SELECT revision, flag, owner_stage "
                "FROM dirty_flag_clear_receipts ORDER BY revision"
            ).fetchall():
                clear_revisions[(str(row["flag"]), int(row["owner_stage"]))].append(
                    int(row["revision"])
                )
        for row in connection.execute(
            "SELECT * FROM dirty_causes ORDER BY cause_revision, cause_id"
        ).fetchall():
            record = dict(row)
            key = (str(record["flag"]), int(record["owner_stage"]))
            cause_revision = int(record["cause_revision"])
            if any(revision >= cause_revision for revision in clear_revisions.get(key, ())):
                continue
            prior = reconstructed.get(key)
            if prior is None or int(prior["cause_revision"]) <= cause_revision:
                reconstructed[key] = record

    obligations = dict(active_rows)
    for key, record in reconstructed.items():
        current = obligations.get(key)
        if current is None or int(current["cause_revision"]) < int(record["cause_revision"]):
            obligations[key] = record
    changed: list[dict[str, Any]] = []
    old_hashes: set[str] = set()
    for (flag, owner_stage), record in sorted(obligations.items()):
        old_hash = str(record.get("classifier_contract_sha256") or "UNKNOWN")
        row = active_rows.get((flag, owner_stage))
        needs_insert = row is None
        needs_rebase = needs_insert or old_hash != current_classifier
        if not needs_rebase:
            continue
        old_hashes.add(old_hash)
        connection.execute(
            """
            INSERT INTO dirty_flags(
                flag, owner_stage, cause_revision, cause_artifact,
                baseline_fingerprint, current_fingerprint,
                classifier_contract_sha256
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(flag, owner_stage) DO UPDATE SET
                cause_revision=excluded.cause_revision,
                cause_artifact=excluded.cause_artifact,
                baseline_fingerprint=excluded.baseline_fingerprint,
                current_fingerprint=excluded.current_fingerprint,
                classifier_contract_sha256=excluded.classifier_contract_sha256
            """,
            (
                flag,
                owner_stage,
                int(record["cause_revision"]),
                str(record["cause_artifact"]),
                str(record["baseline_fingerprint"]),
                str(record["current_fingerprint"]),
                current_classifier,
            ),
        )
        changed.append(
            {
                "flag": flag,
                "owner_stage": owner_stage,
                "cause_revision": int(record["cause_revision"]),
                "cause_artifact": str(record["cause_artifact"]),
                "old_classifier_sha256": old_hash,
                "new_classifier_sha256": current_classifier,
                "reconstructed_from_causes": needs_insert,
            }
        )
    if not changed:
        return None
    identity = {
        "schema_version": DIRTY_REBASE_SCHEMA,
        "source_schema_version": int(source_schema_version),
        "target_schema_version": int(target_schema_version),
        "old_classifier_sha256": sorted(old_hashes),
        "new_classifier_sha256": current_classifier,
        "obligations": changed,
    }
    rebase_id = canonical_hash(identity)
    receipt = {**identity, "rebase_id": rebase_id}
    connection.execute(
        """
        INSERT OR IGNORE INTO dirty_classifier_rebases(
            rebase_id, source_schema_version, target_schema_version,
            old_classifier_sha256, new_classifier_sha256,
            obligation_count, created_at, receipt_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            rebase_id,
            int(source_schema_version),
            int(target_schema_version),
            json.dumps(sorted(old_hashes), ensure_ascii=True, sort_keys=True),
            current_classifier,
            len(changed),
            int(time.time()),
            json.dumps(receipt, ensure_ascii=True, sort_keys=True),
        ),
    )
    return receipt
