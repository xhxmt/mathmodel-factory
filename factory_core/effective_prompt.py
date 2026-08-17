from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from .workflow_events import canonical_hash


EFFECTIVE_PROMPT_SCHEMA = "factory-effective-prompt-v1"


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _file_identity(path: Path, root: Path) -> dict[str, Any]:
    if path.is_symlink():
        raise ValueError(f"prompt input must not be a symlink: {path}")
    try:
        relative = path.resolve(strict=False).relative_to(root.resolve()).as_posix()
    except ValueError:
        relative = str(path.resolve(strict=False))
    if not path.is_file():
        return {"path": relative, "exists": False, "size": 0, "sha256": "MISSING"}
    data = path.read_bytes()
    return {
        "path": relative,
        "exists": True,
        "size": len(data),
        "sha256": _sha256_bytes(data),
    }


def attempt_key(
    *,
    stage_id: int | None,
    subtask: str | None,
    source_step_id: int,
    attempt: int,
    selected_revision: int,
) -> str:
    owner = (
        f"stage:{int(stage_id)}:{subtask or ''}"
        if stage_id is not None
        else f"step:{int(source_step_id)}"
    )
    return (
        f"{owner}:selected-revision:{int(selected_revision)}:"
        f"attempt:{int(attempt)}"
    )


def model_config_identity(
    factory_root: Path, project_id: str, step_id: int
) -> tuple[str, dict[str, Any]]:
    web = factory_root / "web"
    records = [
        _file_identity(web / "model_config.json", factory_root),
        _file_identity(web / "model_registry.json", factory_root),
        _file_identity(
            factory_root / "factory_core" / "adapters" / "models" / "dispatcher.py",
            factory_root,
        ),
        _file_identity(
            factory_root / "scripts" / "model_dispatch_config.py",
            factory_root,
        ),
    ]
    try:
        from scripts.model_dispatch_config import get_step_model_ids

        assignment = get_step_model_ids(
            web / "model_config.json", project_id, step_id
        )
    except Exception:
        assignment = None
    identity = {
        "project_id": project_id,
        "step_id": int(step_id),
        "resolved_assignment": list(assignment or ()),
        "records": records,
    }
    return canonical_hash(identity), identity


def build_effective_prompt_receipt(
    *,
    project_dir: Path,
    factory_root: Path,
    project_id: str,
    source_step_id: int,
    stage_id: int | None,
    subtask: str | None,
    attempt: int,
    selected_revision: int,
    prompt_template: Path,
    prompt: str,
    researcher_note: str,
) -> dict[str, Any]:
    from .consultation_projection import ensure_all_consultation_projections

    project = project_dir.resolve()
    consultation = ensure_all_consultation_projections(project)
    if not consultation.valid:
        raise ValueError(
            "consultation projection drift: " + "; ".join(consultation.errors)
        )
    human_review = project / "human_review.md"
    human_review_identity = _file_identity(human_review, project)
    template_identity = _file_identity(prompt_template, factory_root)
    config_sha256, config_identity = model_config_identity(
        factory_root, project_id, source_step_id
    )
    consultation_ids = sorted(
        str(decision.get("decision_id") or "")
        for decision in consultation.decisions
        if decision.get("decision_id")
    )
    input_identity = {
        "schema_version": EFFECTIVE_PROMPT_SCHEMA,
        "attempt_key": attempt_key(
            stage_id=stage_id,
            subtask=subtask,
            source_step_id=source_step_id,
            attempt=attempt,
            selected_revision=selected_revision,
        ),
        "stage": stage_id,
        "subtask": subtask,
        "source_step_id": int(source_step_id),
        "attempt": int(attempt),
        "selected_revision": int(selected_revision),
        "consultation_decision_ids": consultation_ids,
        "researcher_note_sha256": _sha256_bytes(researcher_note.encode("utf-8")),
        "human_review_sha256": human_review_identity["sha256"],
        "prompt_template_sha256": template_identity["sha256"],
        "model_config_sha256": config_sha256,
    }
    prompt_inputs_sha256 = canonical_hash(input_identity)
    identity = {
        **input_identity,
        "effective_prompt_sha256": _sha256_bytes(prompt.encode("utf-8")),
        "prompt_inputs_sha256": prompt_inputs_sha256,
        "human_review_identity": human_review_identity,
        "prompt_template_identity": template_identity,
        "model_config_identity": config_identity,
    }
    receipt_id = canonical_hash(identity)
    return {**identity, "receipt_id": receipt_id}


def verify_effective_prompt_receipt(
    stored: Mapping[str, Any], current: Mapping[str, Any]
) -> tuple[bool, tuple[str, ...]]:
    errors: list[str] = []
    if stored.get("schema_version") != EFFECTIVE_PROMPT_SCHEMA:
        errors.append("stored prompt receipt schema is invalid")
    fields = (
        "attempt_key",
        "stage",
        "subtask",
        "source_step_id",
        "attempt",
        "consultation_decision_ids",
        "researcher_note_sha256",
        "human_review_sha256",
        "prompt_template_sha256",
        "model_config_sha256",
        "effective_prompt_sha256",
        "prompt_inputs_sha256",
    )
    for field in fields:
        if stored.get(field) != current.get(field):
            errors.append(f"effective prompt input drift: {field}")
    stored_identity = {
        key: value for key, value in stored.items() if key not in {"receipt_id", "bound_revision"}
    }
    if stored.get("receipt_id") != canonical_hash(stored_identity):
        errors.append("stored prompt receipt identity hash mismatch")
    return not errors, tuple(errors)
