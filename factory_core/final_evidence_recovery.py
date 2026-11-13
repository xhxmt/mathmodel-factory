"""Recognize Stage 9 checkpoints restored by a recorded config-only correction.

The one-off operation that wrote those corrections
(``factory-final-evidence-reclassification-v1`` rebase receipts) has been
retired; projects that already carry such a receipt keep their restored
checkpoints valid through this check.
"""
from __future__ import annotations

import json

from .current_dirty import capture_artifact_manifest, manifest_fingerprint


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
