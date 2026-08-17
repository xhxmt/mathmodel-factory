from __future__ import annotations

import hashlib
import re
from pathlib import Path

from factory_core.artifacts import atomic_write_text
from factory_core.consultation_projection import stage_consultation_answer


def gate_ready(human_review: Path, gate: str) -> bool:
    """Compatibility display predicate only. Workflow authority lives in SQLite."""

    if not human_review.is_file() or human_review.is_symlink():
        return False
    start = f"<!-- FACTORY_CONSULTATION_{re.sub(r'[^A-Za-z0-9._-]', '_', gate)}_START -->"
    content = human_review.read_text(encoding="utf-8", errors="replace")
    return start in content and "SOURCE_OF_TRUTH: .factory/state.db" in content


def _staging_markers(request_id: str) -> tuple[str, str]:
    component = re.sub(r"[^A-Za-z0-9._-]", "_", request_id)
    return (
        f"<!-- FACTORY_CONSULTATION_STAGING_{component}_START -->",
        f"<!-- FACTORY_CONSULTATION_STAGING_{component}_END -->",
    )


def _write_non_authoritative_staging_marker(
    project: Path,
    *,
    request_id: str,
    gate: str,
    answer_sha256: str,
) -> None:
    """Keep legacy Web artifact-ref callers working without publishing READY text."""

    review = project / "human_review.md"
    if review.is_symlink():
        raise ValueError("human_review.md must not be a symlink")
    if review.exists() and not review.is_file():
        raise ValueError("human_review.md must be a regular file")
    existing = (
        review.read_text(encoding="utf-8", errors="replace")
        if review.is_file()
        else "# 人工审核与介入记录\n"
    )
    start, end = _staging_markers(request_id)
    generated = re.compile(
        rf"(?ms)^{re.escape(start)}$.*?^{re.escape(end)}$\n?"
    )
    existing = generated.sub("", existing)
    legacy = re.compile(
        rf"(?ims)^##[ \t]+CONSULT[ \t]+{re.escape(gate)}\b.*?"
        rf"(?=^##[ \t]+|\Z)"
    )
    existing = legacy.sub("", existing).rstrip()
    marker = "\n".join(
        (
            start,
            "CONSULTATION_STAGED_FOR_SQLITE_CAS",
            f"REQUEST_ID: {request_id}",
            f"GATE: {gate}",
            f"ANSWER_SHA256: {answer_sha256}",
            "STATUS: PENDING_SQLITE_CAS",
            "ANSWER: REQUEST_SCOPED_STAGING_ONLY",
            end,
        )
    )
    atomic_write_text(
        review,
        (existing + "\n\n" if existing else "# 人工审核与介入记录\n\n")
        + marker
        + "\n",
    )


def _write_legacy_ready_answer(
    project: Path,
    *,
    gate: str,
    step: int,
    title: str,
    answer: str,
    timestamp: str,
) -> Path:
    """Preserve the frozen no-SQLite Consultation projection contract."""

    normalized = answer.strip()
    if not normalized:
        raise ValueError("consultation answer must not be empty")
    review = project / "human_review.md"
    if review.is_symlink():
        raise ValueError("human_review.md must not be a symlink")
    if review.exists() and not review.is_file():
        raise ValueError("human_review.md must be a regular file")
    existing = (
        review.read_text(encoding="utf-8", errors="replace")
        if review.is_file()
        else ""
    )
    component = re.sub(r"[^A-Za-z0-9._-]", "_", gate)
    generated = re.compile(
        rf"(?ms)^<!-- FACTORY_CONSULTATION_{re.escape(component)}_START -->$"
        rf".*?^<!-- FACTORY_CONSULTATION_{re.escape(component)}_END -->$\n?"
    )
    existing = generated.sub("", existing)
    legacy = re.compile(
        rf"(?ims)^##[ \t]+CONSULT[ \t]+{re.escape(gate)}\b.*?"
        rf"(?=^##[ \t]+|\Z)"
    )
    section = (
        f"## CONSULT {gate} (Step {step}) — STATUS: READY\n"
        f"咨询点：{title}\n"
        f"提交时间: {timestamp}\n\n"
        f"{normalized}"
    )
    if legacy.search(existing):
        updated = legacy.sub(section + "\n\n", existing, count=1).rstrip() + "\n"
    else:
        prefix = existing.rstrip()
        updated = (
            (prefix + "\n\n" if prefix else "# 人工审核与介入记录\n\n")
            + section
            + "\n"
        )
    atomic_write_text(review, updated)
    return review


def write_consultation_answer(
    *,
    project_path: Path,
    gate: str,
    step: int,
    title: str,
    answer: str,
    timestamp: str,
    request_id: str | None = None,
) -> Path:
    """Write through the active authority boundary.

    Engine-controlled projects stage request-scoped evidence before the SQLite
    CAS.  Projects with no engine authority retain the frozen Legacy READY
    projection used by the file-state adapter.
    """

    from factory_core.storage import SQLiteStateStore

    store = SQLiteStateStore(project_path)
    engine_controlled = False
    pending: dict = {}
    if store.exists:
        state = store.load()
        engine_controlled = state.control_mode == "engine"
        pending = state.pending_action or {}
    if not engine_controlled:
        return _write_legacy_ready_answer(
            project_path,
            gate=gate,
            step=step,
            title=title,
            answer=answer,
            timestamp=timestamp,
        )

    if not request_id:
        metadata = pending.get("metadata") or {}
        identity = (
            metadata.get("human_decision")
            if isinstance(metadata, dict)
            else None
        )
        request_id = (
            str(identity.get("request_id") or "")
            if isinstance(identity, dict)
            else ""
        )
    if str(pending.get("gate") or gate) != gate:
        raise ValueError("consultation answer does not match the pending gate")
    if not request_id:
        raise ValueError("consultation answer staging requires request_id")
    staged = stage_consultation_answer(
        project_dir=project_path,
        request_id=request_id,
        gate=gate,
        step=step,
        title=title,
        answer=answer,
        timestamp=timestamp,
    )
    _write_non_authoritative_staging_marker(
        project_path,
        request_id=request_id,
        gate=gate,
        answer_sha256=hashlib.sha256(answer.strip().encode("utf-8")).hexdigest(),
    )
    return staged
