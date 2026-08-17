from __future__ import annotations


def ensure_prompt_receipt_schema(connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS prompt_attempt_inputs (
            receipt_id TEXT PRIMARY KEY,
            attempt_key TEXT NOT NULL UNIQUE,
            stage_id INTEGER,
            subtask TEXT,
            source_step_id INTEGER NOT NULL,
            attempt INTEGER NOT NULL,
            selected_revision INTEGER NOT NULL,
            bound_revision INTEGER NOT NULL,
            effective_prompt_sha256 TEXT NOT NULL,
            prompt_inputs_sha256 TEXT NOT NULL,
            consultation_decision_ids_json TEXT NOT NULL,
            researcher_note_sha256 TEXT NOT NULL,
            human_review_sha256 TEXT NOT NULL,
            prompt_template_sha256 TEXT NOT NULL,
            model_config_sha256 TEXT NOT NULL,
            receipt_json TEXT NOT NULL
        );
        CREATE TRIGGER IF NOT EXISTS prompt_attempt_inputs_append_only_update
        BEFORE UPDATE ON prompt_attempt_inputs
        BEGIN
            SELECT RAISE(ABORT, 'prompt attempt inputs are append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS prompt_attempt_inputs_append_only_delete
        BEFORE DELETE ON prompt_attempt_inputs
        BEGIN
            SELECT RAISE(ABORT, 'prompt attempt inputs are append-only');
        END;
        """
    )
