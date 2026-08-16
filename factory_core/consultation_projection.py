from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .artifacts import atomic_write_text
from .decision_receipts import verify_decision_receipt
from .storage import SQLiteStateStore


CONSULTATION_PROJECTION_SCHEMA = "factory-consultation-projection-v1"


@dataclass(frozen=True)
class ConsultationProjectionVerification:
    valid: bool
    errors: tuple[str, ...]
    decisions: tuple[dict[str, Any], ...]


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


def render_consultation_projection(decision: Mapping[str, Any]) -> str:
    gate = str(decision.get("gate") or "").strip()
    answer = _answer(decision)
    if not gate or not answer:
        raise ValueError("consultation projection requires a gate and exact answer")
    start, end = _markers(gate)
    answer_sha256 = hashlib.sha256(answer.encode("utf-8")).hexdigest()
    return "\n".join(
        (
            start,
            f"## CONSULT {gate} — STATUS: READY",
            f"SCHEMA: {CONSULTATION_PROJECTION_SCHEMA}",
            f"REQUEST_ID: {decision.get('request_id') or ''}",
            f"DECISION_ID: {decision.get('decision_id') or ''}",
            f"ANSWER_SHA256: {answer_sha256}",
            "SOURCE_OF_TRUTH: .factory/state.db",
            "PROJECTION: REBUILDABLE_DO_NOT_EDIT",
            "ANSWER_BEGIN",
            answer,
            "ANSWER_END",
            end,
        )
    )


def extract_ready_consultation_answer(
    project_dir: str | Path, gate: str
) -> str | None:
    """Read the exact answer from a READY compatibility section before commit."""

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


def write_pending_consultation_answer(
    *,
    project_dir: str | Path,
    gate: str,
    step: int,
    title: str,
    answer: str,
    timestamp: str,
) -> None:
    """Write pre-commit Web/CLI evidence without leaving stale projection markers."""

    project = Path(project_dir).resolve()
    normalized_answer = answer.strip()
    if not normalized_answer:
        raise ValueError("consultation answer must not be empty")
    section = (
        f"## CONSULT {gate} (Step {step}) — STATUS: READY\n"
        f"咨询点：{title}\n"
        f"提交时间: {timestamp}\n\n"
        f"{normalized_answer}"
    )
    review = project / "human_review.md"
    existing = (
        review.read_text(encoding="utf-8", errors="replace")
        if review.is_file() and not review.is_symlink()
        else ""
    )
    atomic_write_text(review, _replace_section(existing, gate, section))


def rebuild_consultation_projection(
    project_dir: str | Path,
    gate: str,
    decision: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    project = Path(project_dir).resolve()
    store = SQLiteStateStore(project)
    authoritative = dict(decision or (store.decision(gate) if store.exists else {}) or {})
    if not authoritative or authoritative.get("kind") != "consultation":
        raise ValueError(f"no authoritative consultation decision exists for {gate}")
    receipt = verify_decision_receipt(project, authoritative)
    if not receipt.valid:
        raise ValueError(
            "invalid consultation decision receipt: " + "; ".join(receipt.errors)
        )
    rendered = render_consultation_projection(authoritative)
    review = project / "human_review.md"
    existing = (
        review.read_text(encoding="utf-8", errors="replace")
        if review.is_file() and not review.is_symlink()
        else ""
    )
    atomic_write_text(review, _replace_section(existing, gate, rendered))
    return authoritative


def verify_consultation_projections(
    project_dir: str | Path,
) -> ConsultationProjectionVerification:
    project = Path(project_dir).resolve()
    store = SQLiteStateStore(project)
    history = store.decision_history() if store.exists else []
    latest: dict[str, dict[str, Any]] = {}
    for decision in history:
        if decision.get("kind") == "consultation":
            latest[str(decision.get("gate") or "")] = dict(decision)
    if not latest:
        return ConsultationProjectionVerification(True, (), ())
    review = project / "human_review.md"
    text = (
        review.read_text(encoding="utf-8", errors="replace")
        if review.is_file() and not review.is_symlink()
        else ""
    )
    errors: list[str] = []
    decisions: list[dict[str, Any]] = []
    for gate, historical in sorted(latest.items()):
        current = store.decision(gate)
        if current is None or current.get("decision_id") != historical.get("decision_id"):
            errors.append(f"{gate}: authoritative consultation decision is stale or invalid")
            continue
        receipt = verify_decision_receipt(project, current)
        if not receipt.valid:
            errors.extend(f"{gate}: receipt: {error}" for error in receipt.errors)
            continue
        try:
            rendered = render_consultation_projection(current)
        except ValueError as exc:
            errors.append(f"{gate}: {exc}")
            continue
        start, end = _markers(gate)
        match = re.search(
            rf"(?ms)^{re.escape(start)}$.*?^{re.escape(end)}$",
            text,
        )
        if match is None or match.group(0) != rendered:
            errors.append(f"{gate}: human_review.md is not the deterministic SQLite projection")
            continue
        decisions.append(dict(current))
    return ConsultationProjectionVerification(
        not errors, tuple(errors), tuple(decisions)
    )


def authoritative_consultation_prompt(project_dir: str | Path) -> str:
    verification = verify_consultation_projections(project_dir)
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
        answer = _answer(decision)
        lines.extend(
            (
                f"GATE: {decision.get('gate')}",
                f"REQUEST_ID: {decision.get('request_id')}",
                f"DECISION_ID: {decision.get('decision_id')}",
                "ANSWER_BEGIN",
                answer,
                "ANSWER_END",
            )
        )
    return "\n".join(lines) + "\n\n"
