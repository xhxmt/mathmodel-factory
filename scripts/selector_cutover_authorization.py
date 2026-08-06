#!/usr/bin/env python3
"""Validate a human-issued, limited selector cutover authorization receipt.

The validator never creates an authorization and never changes workflow
routing. It only checks that a supplied receipt is current, scoped, and bound
to ready R0a/R0b/R3 reports.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:
    from scripts.capability_harness import (
        _is_sha256,
        _read_json,
        _write_json,
        canonical_hash,
        evaluator_fingerprint,
        evaluator_identity,
        file_sha256,
    )
    from scripts.hard_gate_calibration import REPORT_SCHEMA as R0A_REPORT_SCHEMA
    from scripts.selector_calibration import REPORT_SCHEMA as R0B_REPORT_SCHEMA
    from scripts.shadow_portfolio import REPORT_SCHEMA as R3_REPORT_SCHEMA
except ModuleNotFoundError:  # Direct execution from scripts/.
    from capability_harness import (  # type: ignore
        _is_sha256,
        _read_json,
        _write_json,
        canonical_hash,
        evaluator_fingerprint,
        evaluator_identity,
        file_sha256,
    )
    from hard_gate_calibration import REPORT_SCHEMA as R0A_REPORT_SCHEMA  # type: ignore
    from selector_calibration import REPORT_SCHEMA as R0B_REPORT_SCHEMA  # type: ignore
    from shadow_portfolio import REPORT_SCHEMA as R3_REPORT_SCHEMA  # type: ignore


RECEIPT_SCHEMA = "selector-cutover-authorization-v1"
ASSESSMENT_SCHEMA = "selector-cutover-authorization-assessment-v1"


class AuthorizationError(ValueError):
    """Raised when an authorization receipt is invalid or stale."""


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AuthorizationError(f"{field} must be a non-empty string")
    return value


def _hash(value: Any, field: str) -> str:
    if not _is_sha256(value):
        raise AuthorizationError(f"{field} must be a lowercase SHA-256")
    return value


def _positive_int(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise AuthorizationError(f"{field} must be a positive integer")
    return value


def _timestamp(value: Any, field: str) -> datetime:
    text = _text(value, field)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AuthorizationError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise AuthorizationError(f"{field} must include a timezone")
    return parsed.astimezone(UTC)


def _pinned(root: Path, reference: Any, schema: str, label: str) -> tuple[dict[str, Any], str]:
    if not isinstance(reference, dict) or set(reference) != {"path", "sha256"}:
        raise AuthorizationError(f"{label} must contain exactly path and sha256")
    path_value = reference.get("path")
    if not isinstance(path_value, str) or not path_value:
        raise AuthorizationError(f"{label}.path must be a relative string")
    relative = Path(path_value)
    if relative.is_absolute() or ".." in relative.parts:
        raise AuthorizationError(f"{label}.path must stay below the receipt root")
    path = (root / relative).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError as exc:
        raise AuthorizationError(f"{label}.path escapes the receipt root") from exc
    if not path.is_file():
        raise AuthorizationError(f"{label}.path is not a regular file")
    expected = _hash(reference.get("sha256"), f"{label}.sha256")
    if file_sha256(path) != expected:
        raise AuthorizationError(f"{label} is not hash-pinned")
    report = _read_json(path)
    if report.get("schema") != schema:
        raise AuthorizationError(f"{label} schema mismatch")
    return report, expected


def _identity(value: Any, field: str) -> tuple[dict[str, str], str]:
    if not isinstance(value, dict):
        raise AuthorizationError(f"{field} must be an evaluator identity")
    try:
        identity = evaluator_identity(value)
    except Exception as exc:
        raise AuthorizationError(str(exc)) from exc
    return identity, evaluator_fingerprint(identity)


def validate_authorization(
    receipt: dict[str, Any], receipt_root: Path, *, as_of: datetime | None = None
) -> dict[str, Any]:
    if receipt.get("schema") != RECEIPT_SCHEMA:
        raise AuthorizationError(f"receipt schema must be {RECEIPT_SCHEMA}")
    authorization_id = _text(receipt.get("authorization_id"), "authorization_id")
    approved_by = _text(receipt.get("approved_by"), "approved_by")
    reason = _text(receipt.get("reason"), "reason")
    approved_at = _timestamp(receipt.get("approved_at"), "approved_at")
    expires_at = _timestamp(receipt.get("expires_at"), "expires_at")
    if as_of is not None and as_of.tzinfo is None:
        raise AuthorizationError("as_of must include a timezone")
    current = (as_of or datetime.now(UTC)).astimezone(UTC)
    if expires_at <= approved_at:
        raise AuthorizationError("expires_at must be after approved_at")
    if current < approved_at:
        raise AuthorizationError("authorization is not active yet")
    if current >= expires_at:
        raise AuthorizationError("authorization has expired")
    if receipt.get("revoked") is not False:
        raise AuthorizationError("authorization must be explicitly unrevoked")
    if receipt.get("canary_only") is not True:
        raise AuthorizationError("first selector authorization must be canary_only=true")
    if receipt.get("automatic_switch_performed") is not False:
        raise AuthorizationError("authorization receipt must not claim an automatic switch")

    reports = receipt.get("reports")
    if not isinstance(reports, dict) or set(reports) != {"r0a", "r0b", "r3"}:
        raise AuthorizationError("reports must bind exactly r0a, r0b, and r3")
    r0a, r0a_hash = _pinned(receipt_root, reports["r0a"], R0A_REPORT_SCHEMA, "reports.r0a")
    r0b, r0b_hash = _pinned(receipt_root, reports["r0b"], R0B_REPORT_SCHEMA, "reports.r0b")
    r3, r3_hash = _pinned(receipt_root, reports["r3"], R3_REPORT_SCHEMA, "reports.r3")
    if r0a.get("hard_gate_ready") is not True:
        raise AuthorizationError("R0a report is not ready")
    if (
        r0a.get("automatic_switch_performed") is not False
        or r0a.get("operator_authorization_required") is not True
        or r0a.get("claim_limit")
        != "EXACT_RUNTIME_ORACLE_CAPABILITY_AND_REPEATABILITY_ONLY"
    ):
        raise AuthorizationError("R0a report violates advisory-only governance")
    if r0b.get("comparison_ready_human") is not True:
        raise AuthorizationError("R0b human selector report is not ready")
    if (
        r0b.get("advisory_only") is not True
        or r0b.get("automatic_switch_performed") is not False
        or r0b.get("operator_authorization_required") is not True
        or r0b.get("production_selection_authorized") is not False
        or r0b.get("claim_limit") != "BLIND_PAIRWISE_SELECTOR_CALIBRATION_ONLY"
    ):
        raise AuthorizationError("R0b report violates advisory-only governance")
    _hash(r0b.get("holdout_hash"), "R0b holdout_hash")
    if r3.get("portfolio_ready") is not True:
        raise AuthorizationError("R3 shadow portfolio report is not ready")
    if r3.get("r0a_report_sha256") != r0a_hash or r3.get("selector_report_sha256") != r0b_hash:
        raise AuthorizationError("R3 report does not bind the supplied R0a/R0b reports")
    if (
        r3.get("advisory_only") is not True
        or r3.get("automatic_switch_performed") is not False
        or r3.get("operator_authorization_required") is not True
        or r3.get("production_selection_authorized") is not False
        or r3.get("claim_limit") != "SHADOW_PORTFOLIO_RECOMMENDATION_ONLY"
        or r3.get("gate2_isolated") is not True
        or r3.get("selector_labels_from_gate2") is not False
    ):
        raise AuthorizationError("R3 report violates pre-authorization governance")
    _hash(
        r3.get("gate2_evaluator_identity_fingerprint"),
        "R3 gate2_evaluator_identity_fingerprint",
    )
    _hash(r3.get("gate2_isolation_receipt_sha256"), "R3 gate2_isolation_receipt_sha256")
    if r3.get("gate2_hidden_fields") != [
        "selector_recommendation",
        "candidate_scores",
        "rejected_candidate_identity",
    ]:
        raise AuthorizationError("R3 report does not bind the required Gate 2 hidden fields")

    hard_identity, hard_fp = _identity(receipt.get("hard_gate_evaluator"), "hard_gate_evaluator")
    selector_identity, selector_fp = _identity(receipt.get("selector_evaluator"), "selector_evaluator")
    if r0a.get("evaluator") != hard_identity or r0a.get("evaluator_identity_fingerprint") != hard_fp:
        raise AuthorizationError("receipt hard-gate identity differs from R0a")
    if r0b.get("evaluator") != selector_identity or r0b.get("evaluator_identity_fingerprint") != selector_fp:
        raise AuthorizationError("receipt selector identity differs from R0b")
    if r3.get("hard_gate_identity_fingerprint") != hard_fp or r3.get("selector_identity_fingerprint") != selector_fp:
        raise AuthorizationError("R3 evaluator identities differ from the receipt")
    if r0b.get("hard_gate_identity_fingerprint") != hard_fp:
        raise AuthorizationError("R0b report does not bind the supplied R0a identity")

    scope = receipt.get("scope")
    if not isinstance(scope, dict):
        raise AuthorizationError("scope must be an object")
    required_scope = {
        "workflow_steps",
        "project_ids",
        "problem_types",
        "maximum_k",
        "budget_policy_sha256",
        "packet_builder_sha256",
        "tie_band",
    }
    if set(scope) != required_scope:
        raise AuthorizationError(f"scope must configure exactly {sorted(required_scope)}")
    steps = scope["workflow_steps"]
    if not isinstance(steps, list) or not steps or any(
        not isinstance(step, int) or isinstance(step, bool) or step < 0 or step > 16
        for step in steps
    ):
        raise AuthorizationError("scope.workflow_steps must be explicit steps 0-16")
    if len(set(steps)) != len(steps):
        raise AuthorizationError("scope.workflow_steps must be unique")
    for field in ("project_ids", "problem_types"):
        values = scope[field]
        if not isinstance(values, list) or not values or any(
            not isinstance(value, str) or not value.strip() for value in values
        ):
            raise AuthorizationError(f"scope.{field} must be a non-empty string array")
    maximum_k = _positive_int(scope["maximum_k"], "scope.maximum_k")
    _hash(scope["budget_policy_sha256"], "scope.budget_policy_sha256")
    _hash(scope["packet_builder_sha256"], "scope.packet_builder_sha256")
    tie_band = scope["tie_band"]
    if isinstance(tie_band, bool) or not isinstance(tie_band, (int, float)):
        raise AuthorizationError("scope.tie_band must be numeric")
    if float(tie_band) != float(r0b.get("tie_band")) or float(tie_band) != float(r3.get("tie_band")):
        raise AuthorizationError("scope.tie_band differs from R0b/R3")

    return {
        "schema": ASSESSMENT_SCHEMA,
        "generated_at": datetime.now(UTC).isoformat(),
        "as_of": current.isoformat(),
        "authorization_receipt_sha256": canonical_hash(receipt),
        "authorization_id": authorization_id,
        "approved_by": approved_by,
        "reason": reason,
        "approved_at": approved_at.isoformat(),
        "expires_at": expires_at.isoformat(),
        "report_sha256": {"r0a": r0a_hash, "r0b": r0b_hash, "r3": r3_hash},
        "hard_gate_identity_fingerprint": hard_fp,
        "selector_identity_fingerprint": selector_fp,
        "scope": {**scope, "maximum_k": maximum_k, "tie_band": float(tie_band)},
        "authorization_valid": True,
        "canary_only": True,
        "automatic_switch_performed": False,
        "route_change_event_required": True,
        "claim_limit": "LIMITED_SELECTOR_CUTOVER_AUTHORIZATION_ONLY",
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("receipt", type=Path)
    parser.add_argument("--json-output", required=True, type=Path)
    parser.add_argument("--as-of", help="ISO-8601 time for reproducible validation")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        receipt_path = args.receipt.resolve()
        as_of = _timestamp(args.as_of, "as_of") if args.as_of else None
        report = validate_authorization(
            _read_json(receipt_path), receipt_path.parent, as_of=as_of
        )
        output = args.json_output.resolve()
        _write_json(output, report)
        print(output)
        return 0
    except AuthorizationError as exc:
        print(f"selector authorization rejected input: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
