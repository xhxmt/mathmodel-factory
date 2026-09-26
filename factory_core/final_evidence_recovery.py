"""Recover a proved config-only false reopen without issuing success receipts."""
from __future__ import annotations

import json

from .current_artifact_ownership import artifact_ownership
from .current_dirty import capture_artifact_manifest, classifier_contract_sha256, manifest_fingerprint
from .domain import InvalidTransition, RevisionConflict, SCHEMA_VERSION
from .workflow_events import canonical_hash


def classifier_equivalent_for_restored_checkpoints(store, checkpoints, classifier):
    """Recognize only checkpoint bytes covered by a verified config correction."""
    with store._session() as connection:
        records = [json.loads(r[0]) for r in connection.execute(
            "SELECT receipt_json FROM dirty_classifier_rebases ORDER BY created_at DESC")]
        records = [r for r in records if r.get("schema_version") == "factory-final-evidence-reclassification-v1"
                   and r["new_classifier_sha256"] == classifier]
        for receipt in records:
            matched = True
            for checkpoint in checkpoints:
                row = connection.execute("""SELECT * FROM stage_checkpoint_history
                    WHERE stage_id=? AND subtask=? AND completed_revision=?""",
                    (checkpoint["stage_id"], checkpoint["subtask"], checkpoint["completed_revision"])).fetchone()
                if (row is None or row["checkpoint_id"] not in receipt["restored_checkpoint_ids"]
                        or json.loads(row["receipt_json"]) != checkpoint["receipt"]):
                    matched = False
                    break
            if not matched:
                continue
            manifest = capture_artifact_manifest(store.project_dir)
            if (manifest.pop("judge_evidence.json", None) == receipt["config_sha256"]
                    and manifest_fingerprint(manifest) == receipt["unchanged_business_fingerprint"]):
                return True
    return False


def recover_final_evidence_config(store, *, expected_revision: int, source_revision: int):
    """Restore authentic pre-reopen checkpoints; retain a final-audit obligation.

    This deliberately supports only an added judge_evidence.json whose previous
    unknown-artifact classification invalidated already completed Steps 5-15.
    Any other changed artifact, checkpoint, obligation, or live runner fails.
    Historical receipts (including skipped/failed reviews) are copied unchanged.
    """
    def require(condition, message):
        if not condition:
            raise InvalidTransition(message)

    artifact = "judge_evidence.json"
    owner = artifact_ownership(artifact)
    require(owner is not None and owner.owner_stage == 10
            and owner.dirty_flag == "FORMAT_DIRTY", "final evidence ownership missing")
    now = int(store._clock())
    with store._session() as connection:
        store._upgrade_schema(connection)
        connection.execute("BEGIN IMMEDIATE")
        before = connection.execute("SELECT * FROM project_state WHERE singleton=1").fetchone()
        if before["revision"] != expected_revision:
            raise RevisionConflict("recovery revision changed")
        require(before["status"] == "paused" and before["runner_pid"] is None,
                "recovery requires paused project without runner")
        source = connection.execute("SELECT * FROM events WHERE revision=?", (source_revision,)).fetchone()
        require(source is not None and source_revision < expected_revision, "missing historical event")
        source_payload = json.loads(source["payload_json"])
        expected_checkpoint_hash = source_payload["_workflow"]["effect_hashes_after"]["stage_checkpoints"]
        history = [dict(r) for r in connection.execute(
            "SELECT * FROM stage_checkpoint_history ORDER BY completed_revision, checkpoint_id")]
        require(history and max(r["completed_revision"] for r in history) <= source_revision,
                "a later checkpoint exists")
        latest = {}
        for checkpoint in history:
            latest[(checkpoint["stage_id"], checkpoint["subtask"])] = checkpoint
        authentic = [{k: v for k, v in latest[key].items() if k != "checkpoint_id"}
                     for key in sorted(latest)]
        require(canonical_hash(authentic) == expected_checkpoint_hash,
                "historical checkpoint set does not match recorded active set")
        terminal = max(authentic, key=lambda c: c["completed_revision"])
        require(terminal["completed_step_id"] == 15 and terminal["subtask"] == "polish",
                "last authentic checkpoint must be Step15 polish")
        manifest = capture_artifact_manifest(store.project_dir)
        config_hash = manifest.get(artifact)
        require(config_hash is not None, "final evidence config missing")
        unchanged = {k: v for k, v in manifest.items() if k != artifact}
        require(manifest_fingerprint(unchanged) == terminal["output_fingerprint"],
                "business artifacts changed since Step15")
        active = [dict(r) for r in connection.execute("SELECT * FROM dirty_flags")]
        follow_on = len(active) == 1 and active[0]["flag"] == "FORMAT_DIRTY" and active[0]["owner_stage"] == 10
        if follow_on:
            corrections = [json.loads(r[0]) for r in connection.execute("SELECT receipt_json FROM dirty_classifier_rebases")]
            corrections = [r for r in corrections if r.get("schema_version") == "factory-final-evidence-reclassification-v1"
                           and r["config_sha256"] == config_hash and r["source_revision"] == source_revision
                           and r["new_classifier_sha256"] == classifier_contract_sha256()]
            require(corrections, "final obligation has no verified config correction")
            ids = corrections[-1]["corrected_cause_ids"]
            causes = [dict(r) for r in connection.execute("SELECT * FROM dirty_causes WHERE cause_id IN (?,?)", ids)]
        else:
            require(len(active) == 2 and {(r["flag"], r["owner_stage"]) for r in active}
                    == {("MATH_DIRTY", 8), ("RESULT_DIRTY", 4)}, "unexpected dirty obligations")
        require(all(r["cause_artifact"] == artifact and r["baseline_fingerprint"] == "MISSING"
                    and r["current_fingerprint"] == config_hash for r in active),
                "dirty causes are not exactly the added evidence config")
        revisions = {r["cause_revision"] for r in (causes if follow_on else active)}
        require(len(revisions) == 1 and min(revisions) > source_revision, "invalid cause revision")
        cause_revision = min(revisions)
        cause_event = connection.execute("SELECT type FROM events WHERE revision=?", (cause_revision,)).fetchone()
        require(cause_event is not None and cause_event[0] == "STAGE_SEMANTIC_REOPENED",
                "cause was not a semantic reopen")
        causes = [dict(r) for r in connection.execute("SELECT * FROM dirty_causes WHERE cause_revision=?", (cause_revision,))]
        require(len(causes) == 2 and all(r["cause_artifact"] == artifact for r in causes),
                "unexpected historical causes")
        revision = expected_revision + 1
        classifier = classifier_contract_sha256()
        receipt = {
            "schema_version": "factory-final-evidence-reclassification-v1",
            "source_revision": source_revision, "revision": revision,
            "restored_checkpoint_hash": expected_checkpoint_hash,
            "unchanged_business_fingerprint": terminal["output_fingerprint"],
            "config_sha256": config_hash,
            "corrected_cause_ids": sorted(r["cause_id"] for r in causes),
            "restored_checkpoint_ids": [latest[k]["checkpoint_id"] for k in sorted(latest)],
            "old_classifier_sha256": sorted({r["classifier_contract_sha256"] for r in causes}),
            "new_classifier_sha256": classifier,
            "remaining_obligation": {"flag": "FORMAT_DIRTY", "owner_stage": 10},
            "quality_override": False, "new_success_receipts": 0,
        }
        receipt_json = json.dumps(receipt, ensure_ascii=True, sort_keys=True)
        connection.execute("INSERT INTO dirty_classifier_rebases VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                           (canonical_hash(receipt), SCHEMA_VERSION, SCHEMA_VERSION,
                            json.dumps(receipt["old_classifier_sha256"]), classifier, 1, now, receipt_json))
        connection.execute("DELETE FROM dirty_flags")
        final_cause_id = canonical_hash({"recovery": receipt, "artifact": artifact})[:32]
        values = ("FORMAT_DIRTY", 10, revision, artifact, "MISSING", config_hash, classifier)
        connection.execute("INSERT INTO dirty_causes VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (final_cause_id, *values))
        from .dirty_classification import (
            classification_contract_sha256,
            record_classification,
        )

        # 0.7.1: this path creates a NEW cause, so it must record provenance like
        # any other writer.  Leaving it unrecorded would make a v10-created cause
        # indistinguishable from a pre-v10 historical one, blurring the audit
        # reading of legacy_unrecorded.
        record_classification(
            connection,
            cause_id=final_cause_id,
            classification_source="bespoke_recovery",
            contract_sha256=classification_contract_sha256(),
        )
        connection.execute("INSERT INTO dirty_flags VALUES (?, ?, ?, ?, ?, ?, ?)", values)
        connection.execute("DELETE FROM stage_checkpoints")
        columns = list(authentic[0])
        sql = "INSERT INTO stage_checkpoints(" + ",".join(columns) + ") VALUES (" + ",".join("?" for _ in columns) + ")"
        connection.executemany(sql, [[c[k] for k in columns] for c in authentic])
        connection.execute("DELETE FROM stage_cursor_inputs")
        connection.execute("""UPDATE project_state SET status='ready', last_completed_step=15,
            last_completed_stage=9, active_step=16, active_stage=10,
            active_subtask='content_freeze_guard', source_step_id=16, attempt=0,
            pending_action_json=NULL, runner_pid=NULL, runner_lease_id=NULL,
            heartbeat_at=NULL, revision=?, updated_at=?, last_event_at=? WHERE singleton=1""",
                           (revision, now, now))
        after = connection.execute("SELECT * FROM project_state WHERE singleton=1").fetchone()
        payload = store._versioned_event_payload(connection, before=before, after=after,
            revision=revision, event_type="FINAL_EVIDENCE_CLASSIFICATION_RECOVERED",
            created_at=now, payload=receipt)
        connection.execute("INSERT INTO events(revision,type,created_at,step,attempt,payload_json) VALUES(?,?,?,?,?,?)",
                           (revision, "FINAL_EVIDENCE_CLASSIFICATION_RECOVERED", now, 16, 0,
                            json.dumps(payload, ensure_ascii=True, sort_keys=True)))
    return store._state_from_row(after)
