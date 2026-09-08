from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .artifacts import atomic_write_text
from .decision_receipts import verify_decision_receipt
from .human_decisions import decision_fingerprints
from .storage import SQLiteStateStore


CONSULTATION_PROJECTION_SCHEMA = "factory-consultation-projection-v1"
CONSULTATION_STAGING_SCHEMA = "factory-consultation-answer-staging-v1"


@dataclass(frozen=True)
class ConsultationProjectionVerification:
    valid: bool
    errors: tuple[str, ...]
    decisions: tuple[dict[str, Any], ...]


def _canonical_hash(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _gate_component(gate: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", gate)


def _markers(gate: str) -> tuple[str, str]:
    component = _gate_component(gate)
    return (
        f"<!-- FACTORY_CONSULTATION_{component}_START -->",
        f"<!-- FACTORY_CONSULTATION_{component}_END -->",
    )


def _answer(decision: Mapping[str, Any]) -> str:
    return str(decision.get("answer") or decision.get("response") or "").strip()


def _evidence(decision: Mapping[str, Any]) -> tuple[str, ...]:
    values = decision.get("candidate_evidence") or decision.get("evidence") or ()
    return tuple(str(item) for item in values if isinstance(item, str) and item)


def render_consultation_projection(decision: Mapping[str, Any]) -> str:
    gate = str(decision.get("gate") or "").strip()
    answer = _answer(decision)
    if not gate or not answer:
        raise ValueError("consultation projection requires a gate and exact answer")
    start, end = _markers(gate)
    answer_sha256 = hashlib.sha256(answer.encode("utf-8")).hexdigest()
    # General project readers (including independent judges) receive only a
    # receipt pointer. Joint advisory text is consumed through its own bound
    # modeling path, never injected into the shared human-review preamble.
    joint = gate in {"joint_modeling_candidates", "joint_modeling_risk"}
    projected_answer = (
        "Joint modeling advisory is retained in its immutable decision receipt; "
        "consult the dedicated modeling panel."
        if joint else answer
    )
    return "\n".join(
        (
            start,
            f"## CONSULT {gate} — STATUS: READY",
            f"SCHEMA: {CONSULTATION_PROJECTION_SCHEMA}",
            f"REQUEST_ID: {decision.get('request_id') or ''}",
            f"DECISION_ID: {decision.get('decision_id') or ''}",
            f"SUBJECT_FINGERPRINT: {decision.get('subject_fingerprint') or ''}",
            f"OPTIONS_FINGERPRINT: {decision.get('options_fingerprint') or ''}",
            f"ANSWER_SHA256: {answer_sha256}",
            "SOURCE_OF_TRUTH: .factory/state.db",
            "PROJECTION: REBUILDABLE_DO_NOT_EDIT",
            "ANSWER_BEGIN",
            projected_answer,
            "ANSWER_END",
            end,
        )
    )


def extract_ready_consultation_answer(
    project_dir: str | Path, gate: str
) -> str | None:
    """Read legacy READY text only after a pending immutable request exists."""

    project = Path(project_dir).resolve()
    review = project / "human_review.md"
    if not review.is_file() or review.is_symlink():
        return None
    text = review.read_text(encoding="utf-8", errors="replace")
    start, end = _markers(gate)
    generated = re.search(
        rf"{re.escape(start)}.*?^ANSWER_BEGIN\s*$\n(?P<answer>.*?)"
        rf"\n^ANSWER_END\s*$.*?{re.escape(end)}",
        text,
        re.MULTILINE | re.DOTALL,
    )
    if generated:
        return generated.group("answer").strip() or None
    section = re.search(
        rf"(?ims)^##[ \t]+CONSULT[ \t]+{re.escape(gate)}\b"
        rf"(?P<header>[^\n]*STATUS:[ \t]*READY[^\n]*)\n"
        rf"(?P<body>.*?)(?=^##[ \t]+|\Z)",
        text,
    )
    if section is None:
        return None
    body = section.group("body").strip()
    if not body:
        return None
    lines = body.splitlines()
    while lines and re.match(
        r"^(?:咨询点|提交时间|title|timestamp)[：:]", lines[0], re.I
    ):
        lines.pop(0)
    while lines and not lines[0].strip():
        lines.pop(0)
    body = "\n".join(lines).strip()
    return body or None


def _remove_staging_marker(text: str, request_id: str) -> str:
    component = re.sub(r"[^A-Za-z0-9._-]", "_", request_id)
    start = f"<!-- FACTORY_CONSULTATION_STAGING_{component}_START -->"
    end = f"<!-- FACTORY_CONSULTATION_STAGING_{component}_END -->"
    return re.sub(
        rf"(?ms)^{re.escape(start)}$.*?^{re.escape(end)}$\n?",
        "",
        text,
    ).rstrip()


def _replace_section(text: str, gate: str, rendered: str) -> str:
    start, end = _markers(gate)
    generated_pattern = re.compile(
        rf"(?ms)^{re.escape(start)}$.*?^{re.escape(end)}$\n?"
    )
    if generated_pattern.search(text):
        return generated_pattern.sub(rendered + "\n", text, count=1).rstrip() + "\n"
    legacy_pattern = re.compile(
        rf"(?ims)^##[ \t]+CONSULT[ \t]+{re.escape(gate)}\b.*?"
        rf"(?=^##[ \t]+|\Z)"
    )
    if legacy_pattern.search(text):
        return legacy_pattern.sub(rendered + "\n\n", text, count=1).rstrip() + "\n"
    prefix = text.rstrip()
    return (prefix + "\n\n" if prefix else "# 人工审核与介入记录\n\n") + rendered + "\n"


def consultation_staging_path(
    project_dir: str | Path, request_id: str
) -> Path:
    project = Path(project_dir).resolve()
    component = re.sub(r"[^A-Za-z0-9._-]", "_", request_id)
    if not component or component != request_id:
        raise ValueError("consultation staging requires a safe request id")
    path = (
        project
        / ".factory"
        / "decision_staging"
        / "consultation"
        / f"{component}.json"
    )
    cursor = project
    for part in path.relative_to(project).parts[:-1]:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ValueError(
                "consultation staging path traverses a symlink"
            )
    if path.is_symlink():
        raise ValueError("consultation staging receipt must not be a symlink")
    return path


def stage_consultation_answer(
    *,
    project_dir: str | Path,
    request_id: str,
    gate: str,
    answer: str,
    step: int | None = None,
    title: str = "",
    timestamp: str = "",
) -> Path:
    normalized = answer.strip()
    if not normalized:
        raise ValueError("consultation answer must not be empty")
    identity = {
        "schema_version": CONSULTATION_STAGING_SCHEMA,
        "request_id": request_id,
        "gate": gate,
        "step": step,
        "title": title,
        "timestamp": timestamp,
        "answer": normalized,
        "answer_sha256": hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
    }
    payload = {**identity, "content_sha256": _canonical_hash(identity)}
    path = consultation_staging_path(project_dir, request_id)
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded_bytes = encoded.encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        if path.is_symlink() or not path.is_file():
            raise ValueError("consultation staging path is unsafe")
        if path.read_bytes() != encoded_bytes:
            raise ValueError(
                "immutable consultation staging receipt already differs"
            )
        return path
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded_bytes)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            directory = os.open(path.parent, os.O_RDONLY)
        except OSError:
            directory = None
        if directory is not None:
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path


def read_staged_consultation_answer(
    project_dir: str | Path, request_id: str, gate: str
) -> dict[str, Any]:
    path = consultation_staging_path(project_dir, request_id)
    if not path.is_file() or path.is_symlink():
        raise ValueError("consultation staging receipt is missing or unsafe")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid consultation staging receipt: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("consultation staging receipt root must be an object")
    identity = {key: value for key, value in payload.items() if key != "content_sha256"}
    if payload.get("schema_version") != CONSULTATION_STAGING_SCHEMA:
        raise ValueError("consultation staging receipt schema is invalid")
    if payload.get("request_id") != request_id or payload.get("gate") != gate:
        raise ValueError("consultation staging receipt identity mismatch")
    if payload.get("content_sha256") != _canonical_hash(identity):
        raise ValueError("consultation staging receipt content hash mismatch")
    answer = str(payload.get("answer") or "").strip()
    if not answer or payload.get("answer_sha256") != hashlib.sha256(
        answer.encode("utf-8")
    ).hexdigest():
        raise ValueError("consultation staging receipt answer hash mismatch")
    return payload


def write_pending_consultation_answer(
    *,
    project_dir: str | Path,
    gate: str,
    step: int,
    title: str,
    answer: str,
    timestamp: str,
    request_id: str | None = None,
) -> Path:
    """Compatibility API that now stages under immutable request identity."""

    if not request_id:
        raise ValueError("consultation answer staging requires request_id")
    return stage_consultation_answer(
        project_dir=project_dir,
        request_id=request_id,
        gate=gate,
        step=step,
        title=title,
        answer=answer,
        timestamp=timestamp,
    )


def _request_metadata(request: Mapping[str, Any]) -> Mapping[str, Any]:
    metadata = request.get("metadata")
    if isinstance(metadata, Mapping):
        return metadata
    nested = request.get("request")
    if isinstance(nested, Mapping) and isinstance(nested.get("metadata"), Mapping):
        return nested["metadata"]
    return {}


def _reopen_invalidates_consultation(
    store: SQLiteStateStore,
    request: Mapping[str, Any],
    gate: str,
) -> bool:
    from .stages import completed_stage_for_step

    metadata = _request_metadata(request)
    owner_stage = int(
        metadata.get("consultation_owner_stage")
        or (1 if gate == "preflight" else 2 if gate == "step4" else 1)
    )
    requested_revision = int(request.get("requested_revision") or 0)
    for event in store.events(since_revision=requested_revision):
        payload = event.payload or {}
        if event.type == "STAGE_SEMANTIC_REOPENED":
            target = payload.get("semantic_owner_stage")
            if target is not None and int(target) <= owner_stage:
                return True
        if event.type == "STEP_REOPENED":
            resume_after = payload.get("resume_after_step")
            if resume_after is not None:
                next_owner = completed_stage_for_step(int(resume_after)) + 1
                if next_owner <= owner_stage:
                    return True
        if event.type == "STAGE_CHECKPOINT_INVALIDATED":
            invalidated_stage = payload.get("stage")
            if (
                invalidated_stage is not None
                and int(invalidated_stage) <= owner_stage
            ):
                return True
    return False


def current_consultation_decision(
    project_dir: str | Path, gate: str
) -> dict[str, Any] | None:
    project = Path(project_dir).resolve()
    store = SQLiteStateStore(project)
    if not store.exists:
        return None
    decision = store.decision(gate)
    if not decision or decision.get("kind") != "consultation":
        return None
    gate_requests = [
        request
        for request in store.decision_requests()
        if str(
            request.get("gate")
            or request.get("gate_type")
            or ""
        )
        == gate
    ]
    if gate_requests:
        latest = max(
            gate_requests,
            key=lambda item: int(item.get("generation") or 0),
        )
        if (
            str(latest.get("request_id") or "")
            != str(decision.get("request_id") or "")
            or str(latest.get("status") or "") != "resolved"
        ):
            return None
        if _reopen_invalidates_consultation(store, latest, gate):
            return None
    receipt = verify_decision_receipt(project, decision)
    if not receipt.valid:
        return None
    evidence = _evidence(decision)
    if not evidence:
        request_id = str(decision.get("request_id") or "")
        for request in store.decision_requests():
            if str(request.get("request_id") or "") != request_id:
                continue
            values = request.get("evidence") or ()
            if not values and isinstance(request.get("request"), Mapping):
                values = request["request"].get("evidence") or ()
            evidence = tuple(
                str(item)
                for item in values
                if isinstance(item, str) and item
            )
            break
    current_subject, current_options = decision_fingerprints(
        project, gate, evidence
    )
    if current_subject != str(decision.get("subject_fingerprint") or ""):
        return None
    if current_options != str(decision.get("options_fingerprint") or ""):
        return None
    if not _answer(decision):
        return None
    return dict(decision)


def remove_stale_consultation_projection(
    project_dir: str | Path, gate: str
) -> None:
    project = Path(project_dir).resolve()
    review = project / "human_review.md"
    if review.is_symlink():
        raise ValueError("human_review.md must not be a symlink")
    if not review.is_file():
        return
    text = review.read_text(encoding="utf-8", errors="replace")
    start, end = _markers(gate)
    pattern = re.compile(
        rf"(?ms)^{re.escape(start)}$.*?^{re.escape(end)}$\n?"
    )
    updated = pattern.sub("", text).rstrip() + "\n"
    if updated != text:
        atomic_write_text(review, updated)


def rebuild_consultation_projection(
    project_dir: str | Path,
    gate: str,
    decision: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    project = Path(project_dir).resolve()
    authoritative = dict(decision or current_consultation_decision(project, gate) or {})
    if not authoritative:
        raise ValueError(f"no current authoritative consultation decision exists for {gate}")
    receipt = verify_decision_receipt(project, authoritative)
    if not receipt.valid:
        raise ValueError(
            "invalid consultation decision receipt: " + "; ".join(receipt.errors)
        )
    rendered = render_consultation_projection(authoritative)
    review = project / "human_review.md"
    if review.is_symlink():
        raise ValueError("human_review.md must not be a symlink")
    existing = (
        review.read_text(encoding="utf-8", errors="replace")
        if review.is_file()
        else ""
    )
    existing = _remove_staging_marker(
        existing, str(authoritative.get("request_id") or "")
    )
    atomic_write_text(review, _replace_section(existing, gate, rendered))
    return authoritative


def _record_projection_failure(project: Path, gate: str, exc: BaseException) -> None:
    store = SQLiteStateStore(project)
    if not store.exists:
        return
    try:
        state = store.load()
        store.record_projection_failure(
            revision=state.revision,
            projector_name=f"consultation:{gate}",
            error_type=type(exc).__name__,
        )
    except Exception:
        return


def _resolve_projection_failure(project: Path, gate: str) -> None:
    store = SQLiteStateStore(project)
    if not store.exists:
        return
    projector_name = f"consultation:{gate}"
    try:
        failures = store.projection_failures(pending_only=True)
    except Exception:
        return
    for failure in failures:
        if failure.get("projector_name") != projector_name:
            continue
        try:
            store.resolve_projection_failure(
                revision=int(failure["revision"]),
                projector_name=projector_name,
            )
        except Exception:
            pass


def ensure_consultation_projection(
    project_dir: str | Path, gate: str
) -> dict[str, Any] | None:
    project = Path(project_dir).resolve()
    decision = current_consultation_decision(project, gate)
    if decision is None:
        return None
    try:
        rebuilt = rebuild_consultation_projection(project, gate, decision)
    except Exception as exc:
        _record_projection_failure(project, gate, exc)
        raise
    _resolve_projection_failure(project, gate)
    return rebuilt


def verify_consultation_projections(
    project_dir: str | Path,
) -> ConsultationProjectionVerification:
    project = Path(project_dir).resolve()
    store = SQLiteStateStore(project)
    history = store.decision_history() if store.exists else []
    gates = sorted(
        {
            str(decision.get("gate") or "")
            for decision in history
            if decision.get("kind") == "consultation" and decision.get("gate")
        }
    )
    if not gates:
        return ConsultationProjectionVerification(True, (), ())
    review = project / "human_review.md"
    if review.is_symlink():
        return ConsultationProjectionVerification(
            False, ("human_review.md must not be a symlink",), ()
        )
    text = (
        review.read_text(encoding="utf-8", errors="replace")
        if review.is_file()
        else ""
    )
    errors: list[str] = []
    decisions: list[dict[str, Any]] = []
    for gate in gates:
        current = current_consultation_decision(project, gate)
        if current is None:
            start, end = _markers(gate)
            if re.search(
                rf"(?ms)^{re.escape(start)}$.*?^{re.escape(end)}$", text
            ):
                errors.append(
                    f"{gate}: stale consultation projection remains after subject drift"
                )
            continue
        try:
            rendered = render_consultation_projection(current)
        except ValueError as exc:
            errors.append(f"{gate}: {exc}")
            continue
        start, end = _markers(gate)
        match = re.search(rf"(?ms)^{re.escape(start)}$.*?^{re.escape(end)}$", text)
        if match is None or match.group(0) != rendered:
            errors.append(f"{gate}: human_review.md is not the deterministic SQLite projection")
            continue
        decisions.append(current)
    return ConsultationProjectionVerification(not errors, tuple(errors), tuple(decisions))


def ensure_all_consultation_projections(
    project_dir: str | Path,
) -> ConsultationProjectionVerification:
    project = Path(project_dir).resolve()
    store = SQLiteStateStore(project)
    history = store.decision_history() if store.exists else []
    gates = sorted(
        {
            str(item.get("gate") or "")
            for item in history
            if item.get("kind") == "consultation" and item.get("gate")
        }
    )
    for gate in gates:
        if current_consultation_decision(project, gate) is None:
            try:
                remove_stale_consultation_projection(project, gate)
            except Exception as exc:
                _record_projection_failure(project, gate, exc)
                raise
            continue
        ensure_consultation_projection(project, gate)
    return verify_consultation_projections(project)


def authoritative_consultation_prompt(project_dir: str | Path) -> str:
    verification = ensure_all_consultation_projections(project_dir)
    if not verification.valid:
        raise ValueError(
            "consultation projection drift: " + "; ".join(verification.errors)
        )
    if not verification.decisions:
        return ""
    lines = [
        "AUTHORITATIVE CONSULTATION DECISIONS (verified against SQLite):",
        "Use these exact answers; contradictory mutable prose is not authoritative.",
    ]
    for decision in verification.decisions:
        if decision.get("gate") in {"joint_modeling_candidates", "joint_modeling_risk"}:
            # Independent modeling advice is supplied only to the modeling
            # lifecycle. It must never enter final judges' generic preamble.
            continue
        lines.extend(
            (
                f"GATE: {decision.get('gate')}",
                f"REQUEST_ID: {decision.get('request_id')}",
                f"DECISION_ID: {decision.get('decision_id')}",
                f"SUBJECT_FINGERPRINT: {decision.get('subject_fingerprint')}",
                "ANSWER_BEGIN",
                _answer(decision),
                "ANSWER_END",
            )
        )
    return "\n".join(lines) + "\n\n"
