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
    Ordinary artifact causes are then routed through the current ownership
    registry so a classifier release can move an active obligation without
    preserving an obsolete upstream owner.  Only the mutable active index is
    rewritten; causes and historical clear receipts remain byte-for-byte
    historical.
    """

    ensure_dirty_rebase_schema(connection)
    if not _table_exists(connection, "dirty_flags"):
        return None
    from .current_dirty import classifier_contract_sha256, solver_receipt_job_id

    current_classifier = classifier_contract_sha256()
    active_rows = {
        (str(row["flag"]), int(row["owner_stage"])): dict(row)
        for row in connection.execute(
            "SELECT * FROM dirty_flags ORDER BY flag, owner_stage"
        ).fetchall()
    }
    reconstructed: dict[tuple[str, int], dict[str, Any]] = {}
    clear_revisions: dict[tuple[str, int], list[int]] = defaultdict(list)
    if _table_exists(connection, "dirty_causes"):
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

    source_obligations = dict(active_rows)
    for key, record in reconstructed.items():
        current = source_obligations.get(key)
        if current is None or int(current["cause_revision"]) < int(record["cause_revision"]):
            source_obligations[key] = record

    from .current_artifact_ownership import artifact_ownership

    semantic_paper_flags = {
        "MATH_DIRTY",
        "PROSE_DIRTY",
        "CITATION_DIRTY",
        "FORMAT_DIRTY",
    }

    def current_key(
        source_key: tuple[str, int], record: dict[str, Any]
    ) -> tuple[str, int]:
        artifact = str(record["cause_artifact"])
        if artifact.startswith("@protected:"):
            return ("MATH_DIRTY", 8)
        # A paper semantic change stores the real ``*.tex`` path as its cause.
        # Re-routing it through the raw path fallback would lose the domain
        # (prose/citation/format/math), so retain that proven semantic key.
        if artifact.lower().endswith(".tex") and source_key[0] in semantic_paper_flags:
            return source_key
        job_id = solver_receipt_job_id(artifact)
        if job_id is not None and _table_exists(connection, "solver_jobs"):
            job = connection.execute(
                "SELECT owner_stage FROM solver_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            if job is not None and job["owner_stage"] is not None:
                return ("RESULT_DIRTY", int(job["owner_stage"]))
        ownership = artifact_ownership(artifact)
        if ownership is None:
            return source_key
        return (str(ownership.dirty_flag), int(ownership.owner_stage))

    obligations: dict[tuple[str, int], dict[str, Any]] = {}
    for source_key, source_record in source_obligations.items():
        target_key = current_key(source_key, source_record)
        cause_revision = int(source_record["cause_revision"])
        if target_key != source_key and any(
            revision >= cause_revision
            for revision in clear_revisions.get(target_key, ())
        ):
            continue
        record = dict(source_record)
        record["_source_flag"] = source_key[0]
        record["_source_owner_stage"] = source_key[1]
        prior = obligations.get(target_key)
        prior_source = (
            (str(prior["_source_flag"]), int(prior["_source_owner_stage"]))
            if prior is not None
            else None
        )
        if (
            prior is None
            or int(prior["cause_revision"]) < cause_revision
            or (
                int(prior["cause_revision"]) == cause_revision
                and source_key == target_key
                and prior_source != target_key
            )
        ):
            obligations[target_key] = record

    changed: list[dict[str, Any]] = []
    old_hashes = {
        str(record.get("classifier_contract_sha256") or "UNKNOWN")
        for record in source_obligations.values()
    }
    stale_active_keys = sorted(set(active_rows) - set(obligations))
    retired = [
        {
            "flag": flag,
            "owner_stage": owner_stage,
            "cause_artifact": str(active_rows[(flag, owner_stage)]["cause_artifact"]),
        }
        for flag, owner_stage in stale_active_keys
    ]
    for flag, owner_stage in stale_active_keys:
        connection.execute(
            "DELETE FROM dirty_flags WHERE flag=? AND owner_stage=?",
            (flag, owner_stage),
        )
    for (flag, owner_stage), record in sorted(obligations.items()):
        old_hash = str(record.get("classifier_contract_sha256") or "UNKNOWN")
        row = active_rows.get((flag, owner_stage))
        source_flag = str(record.pop("_source_flag"))
        source_owner_stage = int(record.pop("_source_owner_stage"))
        migrated = (source_flag, source_owner_stage) != (flag, owner_stage)
        needs_insert = row is None
        needs_rebase = needs_insert or migrated or old_hash != current_classifier
        if not needs_rebase:
            continue
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
                "previous_flag": source_flag,
                "previous_owner_stage": source_owner_stage,
                "ownership_migrated": migrated,
                "reconstructed_from_causes": (
                    (source_flag, source_owner_stage) not in active_rows
                ),
            }
        )
    if not changed and not retired:
        return None
    identity = {
        "schema_version": DIRTY_REBASE_SCHEMA,
        "source_schema_version": int(source_schema_version),
        "target_schema_version": int(target_schema_version),
        "old_classifier_sha256": sorted(old_hashes),
        "new_classifier_sha256": current_classifier,
        "obligations": changed,
        "retired_obligations": retired,
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
