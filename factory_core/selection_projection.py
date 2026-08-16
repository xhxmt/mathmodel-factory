from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .artifacts import atomic_write_text
from .decision_receipts import verify_decision_receipt
from .human_decisions import decision_fingerprints
from .storage import SQLiteStateStore


STEP3_PROJECTION_SCHEMA = "factory-step3-selection-projection-v1"
_HEADER_START = "<!-- FACTORY_STEP3_DECISION_HEADER_START -->"
_HEADER_END = "<!-- FACTORY_STEP3_DECISION_HEADER_END -->"


@dataclass(frozen=True)
class Step3ProjectionVerification:
    valid: bool
    errors: tuple[str, ...]
    decision: dict[str, Any] | None


def _selection(decision: Mapping[str, Any]) -> tuple[str, str]:
    primary = str(
        decision.get("selected_option_id")
        or decision.get("selected_primary")
        or decision.get("selected")
        or ""
    )
    auxiliary = str(
        decision.get("selected_aux_id")
        or decision.get("selected_auxiliary")
        or "NONE"
    )
    return primary, auxiliary or "NONE"


def _identity_fields(decision: Mapping[str, Any]) -> dict[str, str]:
    primary, auxiliary = _selection(decision)
    return {
        "SCHEMA": STEP3_PROJECTION_SCHEMA,
        "REQUEST_ID": str(decision.get("request_id") or ""),
        "DECISION_ID": str(decision.get("decision_id") or ""),
        "PRIMARY": primary,
        "AUXILIARY": auxiliary,
        "SUBJECT_FINGERPRINT": str(decision.get("subject_fingerprint") or ""),
        "OPTIONS_FINGERPRINT": str(decision.get("options_fingerprint") or ""),
    }


def _header_lines(decision: Mapping[str, Any], *, primary_first: bool) -> list[str]:
    fields = _identity_fields(decision)
    order = (
        ("PRIMARY", "AUXILIARY", "REQUEST_ID", "DECISION_ID", "SUBJECT_FINGERPRINT", "OPTIONS_FINGERPRINT", "SCHEMA")
        if primary_first
        else ("REQUEST_ID", "DECISION_ID", "PRIMARY", "AUXILIARY", "SUBJECT_FINGERPRINT", "OPTIONS_FINGERPRINT", "SCHEMA")
    )
    return [f"{key}: {fields[key]}" for key in order]


def render_chosen_method_projection(decision: Mapping[str, Any]) -> str:
    primary, auxiliary = _selection(decision)
    evidence = [
        str(item)
        for item in decision.get("candidate_evidence") or ()
        if isinstance(item, str) and item
    ]
    lines = [
        *_header_lines(decision, primary_first=True),
        "SOURCE_OF_TRUTH: .factory/state.db",
        "PROJECTION: REBUILDABLE_DO_NOT_EDIT",
        "",
        "## Load-bearing files",
        *(f"- `{path}`" for path in evidence),
        f"- Primary stream: `{primary}`",
        f"- Auxiliary stream: `{auxiliary}`",
        "",
        "This file is generated from the immutable Step 3 SQLite decision.",
    ]
    return "\n".join(lines).rstrip() + "\n"


def _strip_machine_header(text: str) -> str:
    start = text.find(_HEADER_START)
    end = text.find(_HEADER_END)
    if start >= 0 and end >= start:
        return (text[:start] + text[end + len(_HEADER_END) :]).lstrip("\n")
    return text


def render_method_decision_projection(
    decision: Mapping[str, Any], existing_text: str = ""
) -> str:
    body = _strip_machine_header(existing_text).strip()
    header = "\n".join(
        (
            _HEADER_START,
            *_header_lines(decision, primary_first=False),
            _HEADER_END,
        )
    )
    if not body:
        body = (
            "# Step 3 方法选择决策\n\n"
            "机器头由控制平面生成。请在其后补充候选比较、选择理由、风险与下游接力说明。"
        )
    return f"{header}\n\n{body.rstrip()}\n"


def rebuild_step3_projections(
    project_dir: str | Path,
    decision: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    project = Path(project_dir).resolve()
    store = SQLiteStateStore(project)
    authoritative = dict(decision or (store.decision("step3") if store.exists else {}) or {})
    if not authoritative:
        raise ValueError("no authoritative Step 3 decision exists")
    receipt = verify_decision_receipt(project, authoritative)
    if not receipt.valid and not receipt.legacy_unbound:
        raise ValueError("invalid Step 3 decision receipt: " + "; ".join(receipt.errors))
    primary, _auxiliary = _selection(authoritative)
    if not primary:
        raise ValueError("authoritative Step 3 decision has no primary option")
    atomic_write_text(
        project / "chosen_method.md",
        render_chosen_method_projection(authoritative),
    )
    method_path = project / "method_decision.md"
    existing = (
        method_path.read_text(encoding="utf-8", errors="replace")
        if method_path.is_file()
        else ""
    )
    atomic_write_text(
        method_path,
        render_method_decision_projection(authoritative, existing),
    )
    return authoritative


def _parse_fields(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        normalized = key.strip().upper()
        if normalized in {
            "SCHEMA",
            "REQUEST_ID",
            "DECISION_ID",
            "PRIMARY",
            "AUXILIARY",
            "SUBJECT_FINGERPRINT",
            "OPTIONS_FINGERPRINT",
        }:
            fields.setdefault(normalized, value.strip().split()[0] if value.strip() else "")
    return fields


def verify_step3_projections(
    project_dir: str | Path,
) -> Step3ProjectionVerification:
    project = Path(project_dir).resolve()
    store = SQLiteStateStore(project)
    history = store.decision_history("step3") if store.exists else []
    decision = history[-1] if history else None
    if decision is None:
        return Step3ProjectionVerification(
            False, ("authoritative SQLite Step 3 decision is missing",), None
        )
    errors: list[str] = []
    receipt = verify_decision_receipt(project, decision)
    if not receipt.valid:
        errors.extend(f"receipt: {error}" for error in receipt.errors)
    evidence = tuple(
        str(item)
        for item in decision.get("candidate_evidence") or ()
        if isinstance(item, str) and item
    )
    current_subject, current_options = decision_fingerprints(
        project, "step3", evidence
    )
    if current_subject != decision.get("subject_fingerprint"):
        errors.append("candidate evidence fingerprint no longer matches the decision")
    if current_options != decision.get("options_fingerprint"):
        errors.append("Step 3 options fingerprint no longer matches the decision")
    options_path = project / "selection" / "step3_options.json"
    try:
        options_payload = json.loads(options_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        options_payload = {}
    option_ids = {
        str(item.get("id"))
        for item in options_payload.get("options", [])
        if isinstance(item, Mapping) and item.get("id")
    }
    primary, auxiliary = _selection(decision)
    if primary not in option_ids:
        errors.append("selected primary is absent from the bound options")
    if auxiliary != "NONE" and auxiliary not in option_ids:
        errors.append("selected auxiliary is absent from the bound options")
    expected = _identity_fields(decision)
    chosen_path = project / "chosen_method.md"
    chosen_text = (
        chosen_path.read_text(encoding="utf-8", errors="replace")
        if chosen_path.is_file() and not chosen_path.is_symlink()
        else ""
    )
    method_path = project / "method_decision.md"
    method_text = (
        method_path.read_text(encoding="utf-8", errors="replace")
        if method_path.is_file() and not method_path.is_symlink()
        else ""
    )
    expected_method_header = "\n".join(
        (
            _HEADER_START,
            *_header_lines(decision, primary_first=False),
            _HEADER_END,
        )
    )
    if chosen_text and chosen_text != render_chosen_method_projection(decision):
        errors.append("chosen_method.md is not the deterministic SQLite projection")
    if method_text and not method_text.startswith(expected_method_header + "\n"):
        errors.append("method_decision.md machine header is not canonical")
    projected_text = {
        "chosen_method.md": chosen_text,
        "method_decision.md": (
            method_text[: method_text.find(_HEADER_END) + len(_HEADER_END)]
            if method_text.startswith(_HEADER_START)
            and method_text.find(_HEADER_END) >= 0
            else ""
        ),
    }
    for relative, text in projected_text.items():
        actual = _parse_fields(text)
        if not actual:
            errors.append(f"{relative} is missing its machine-verifiable decision header")
            continue
        for key, value in expected.items():
            if actual.get(key) != value:
                errors.append(f"{relative} {key} does not match SQLite decision")
    return Step3ProjectionVerification(not errors, tuple(errors), dict(decision))


def step3_projection_required(project_dir: str | Path) -> bool:
    project = Path(project_dir).resolve()
    store = SQLiteStateStore(project)
    if not store.exists:
        return False
    return store.contest_policy() is not None or bool(
        store.decision_history("step3")
    )
