#!/usr/bin/env python3
"""Route judge outcomes without conflating evidence, packet, and infrastructure failures."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "judge-decision-route-v1"
AGGREGATE_SCHEMA_VERSION = "judge-aggregate-v3"
REQUIRED_ROLES = ("math", "execution", "paper")
DECISIONS = {
    "PASS",
    "REOPEN_REVISION_MODEL",
    "REOPEN_REVISION_TEXT",
    "PACKET_REBUILD",
    "INFRA_RETRY",
    "INDETERMINATE_REVIEW",
}


class RoutingError(ValueError):
    """Raised when routing inputs are malformed."""


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RoutingError(f"invalid routing input: {path}") from exc
    if not isinstance(value, dict):
        raise RoutingError(f"routing input must be an object: {path}")
    return value


def _packet_incomplete(aggregate: dict[str, Any]) -> tuple[bool, list[str]]:
    value = aggregate.get("packet_completeness")
    if not isinstance(value, dict):
        return True, ["aggregate packet completeness is missing"]
    reasons: list[str] = []
    actual_roles = set(value)
    expected_roles = set(REQUIRED_ROLES)
    missing_roles = sorted(expected_roles - actual_roles)
    extra_roles = sorted(actual_roles - expected_roles)
    if missing_roles:
        reasons.append("missing packet roles: " + ",".join(missing_roles))
    if extra_roles:
        reasons.append("unexpected packet roles: " + ",".join(extra_roles))
    for role in REQUIRED_ROLES:
        summary = value.get(role)
        if summary is None:
            continue
        if not isinstance(summary, dict):
            reasons.append(f"{role}: packet summary is invalid")
            continue
        # A route is only judgeable when every production role was checked by
        # the packet contract.  Treating ``enforced=false`` or a missing
        # eligibility bit as complete would let a hand-crafted PASS bypass
        # evidence acquisition.
        if summary.get("enforced") is not True:
            reasons.append(f"{role}: packet completeness was not enforced")
        if summary.get("eligible") is not True:
            unmet = summary.get("unmet_requirements")
            detail = ",".join(str(item) for item in unmet) if isinstance(unmet, list) else "unknown"
            reasons.append(f"{role}: unmet packet requirements ({detail})")
        if summary.get("error"):
            reasons.append(f"{role}: {summary['error']}")
    return bool(reasons), reasons


def _aggregate_integrity(aggregate: dict[str, Any]) -> tuple[bool, list[str]]:
    """Check the fields that make a verdict meaningful before routing it.

    The parser/aggregator is the authority for these relationships.  The
    router nevertheless rechecks them because its output can drive delivery
    and must fail closed when fed a hand-authored or stale JSON object.
    """

    reasons: list[str] = []
    if aggregate.get("schema_version") != AGGREGATE_SCHEMA_VERSION:
        reasons.append("aggregate schema is missing or unsupported")

    statuses = aggregate.get("role_statuses")
    if not isinstance(statuses, dict) or set(statuses) != set(REQUIRED_ROLES):
        reasons.append("aggregate role_statuses must contain exactly math, execution, paper")
        statuses = {}
    allowed = {
        "math": {"PASS", "FAIL", "INDETERMINATE", "LEGACY_UNVERIFIED"},
        "execution": {"PASS", "FAIL", "INDETERMINATE", "LEGACY_UNVERIFIED"},
        "paper": {"PASS", "REVISE", "INDETERMINATE", "LEGACY_UNVERIFIED"},
    }
    for role in REQUIRED_ROLES:
        if role in statuses and statuses[role] not in allowed[role]:
            reasons.append(f"{role}: unsupported role status")

    expected_vetoes = [
        role for role in ("math", "execution") if statuses.get(role) == "FAIL"
    ]
    vetoes = aggregate.get("vetoes")
    if not isinstance(vetoes, list) or sorted(vetoes) != sorted(expected_vetoes):
        reasons.append("aggregate vetoes do not match hard-role FAIL statuses")

    expected_indeterminate = [
        role
        for role in REQUIRED_ROLES
        if statuses.get(role) in {"INDETERMINATE", "LEGACY_UNVERIFIED"}
    ]
    indeterminate = aggregate.get("indeterminate_roles")
    if not isinstance(indeterminate, list) or sorted(indeterminate) != sorted(expected_indeterminate):
        reasons.append("aggregate indeterminate_roles do not match role statuses")

    expected_verdict = None
    if expected_vetoes:
        expected_verdict = "REOPEN_REVISION_MODEL"
    elif expected_indeterminate:
        expected_verdict = "INDETERMINATE_REVIEW"
    elif statuses.get("paper") == "REVISE":
        expected_verdict = "REOPEN_REVISION_TEXT"
    elif set(statuses) == set(REQUIRED_ROLES) and all(
        statuses.get(role) == "PASS" for role in REQUIRED_ROLES
    ):
        expected_verdict = "PASS"
    if expected_verdict is not None and aggregate.get("verdict") != expected_verdict:
        reasons.append(
            f"aggregate verdict {aggregate.get('verdict')!r} contradicts role statuses; expected {expected_verdict}"
        )
    return bool(reasons), reasons


def route_decision(
    aggregate: dict[str, Any] | None,
    visual_gate: dict[str, Any] | None,
    *,
    failure_kind: str | None = None,
    policy_mode: str = "shadow",
) -> dict[str, Any]:
    if policy_mode not in {"shadow", "enforce"}:
        raise RoutingError("policy_mode must be shadow or enforce")
    reasons: list[str] = []
    legacy_decision = aggregate.get("verdict") if isinstance(aggregate, dict) else None

    if failure_kind is not None:
        mapping = {
            "packet": "PACKET_REBUILD",
            "infrastructure": "INFRA_RETRY",
            "indeterminate": "INDETERMINATE_REVIEW",
        }
        if failure_kind not in mapping:
            raise RoutingError("failure_kind must be packet, infrastructure, or indeterminate")
        new_decision = mapping[failure_kind]
        reasons.append(f"explicit {failure_kind} failure before aggregation")
    elif not isinstance(aggregate, dict):
        new_decision = "INFRA_RETRY"
        reasons.append("aggregate is unavailable")
    else:
        incomplete, packet_reasons = _packet_incomplete(aggregate)
        if incomplete:
            new_decision = "PACKET_REBUILD"
            reasons.extend(packet_reasons)
        else:
            integrity_invalid, integrity_reasons = _aggregate_integrity(aggregate)
            if integrity_invalid:
                new_decision = "INDETERMINATE_REVIEW"
                reasons.extend(integrity_reasons)
            else:
                verdict = aggregate.get("verdict")
                if verdict not in {
                    "PASS",
                    "REOPEN_REVISION_MODEL",
                    "REOPEN_REVISION_TEXT",
                    "INDETERMINATE_REVIEW",
                }:
                    new_decision = "INDETERMINATE_REVIEW"
                    reasons.append("aggregate verdict is missing or unsupported")
                else:
                    new_decision = verdict
                    reasons.append(f"aggregate verdict: {verdict}")

        if isinstance(visual_gate, dict):
            visual_status = visual_gate.get("status")
            # Evidence-acquisition failures have priority over content repair.
            # A visual finding cannot make an incomplete packet scientifically
            # judgeable, and it must not convert INDETERMINATE into a guessed
            # text/model diagnosis.
            if new_decision in {
                "PACKET_REBUILD",
                "INDETERMINATE_REVIEW",
                "REOPEN_REVISION_MODEL",
                "REOPEN_REVISION_TEXT",
            }:
                reasons.append(f"visual gate observed while preserving {new_decision}: {visual_status}")
            elif visual_status == "INDETERMINATE":
                new_decision = "INFRA_RETRY"
                reasons.append(f"visual gate indeterminate: {visual_gate.get('error', 'unknown error')}")
            elif visual_status == "FAIL":
                if new_decision != "REOPEN_REVISION_MODEL":
                    new_decision = "REOPEN_REVISION_TEXT"
                codes = [
                    item.get("code")
                    for item in visual_gate.get("findings", [])
                    if isinstance(item, dict) and item.get("severity") == "blocking"
                ]
                reasons.append("visual blocking findings: " + ",".join(str(code) for code in codes))
            elif visual_status != "PASS":
                # Preserve a previously established content/packet diagnosis;
                # a visual report cannot turn a known defect into an
                # infrastructure-only explanation.  A clean aggregate still
                # needs an infrastructure retry when its visual gate is
                # malformed.
                if new_decision == "PASS":
                    new_decision = "INFRA_RETRY"
                reasons.append("visual gate status is missing or invalid")
        else:
            if new_decision == "PASS":
                new_decision = "INFRA_RETRY"
            reasons.append("visual gate report is unavailable")

    if new_decision not in DECISIONS:
        raise RoutingError(f"unsupported routed decision: {new_decision}")
    if policy_mode == "enforce" or legacy_decision not in DECISIONS:
        effective = new_decision
    else:
        effective = legacy_decision
    return {
        "schema": SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "policy_mode": policy_mode,
        "legacy_decision": legacy_decision,
        "new_decision": new_decision,
        "effective_decision": effective,
        "decision_changed": legacy_decision is not None and legacy_decision != new_decision,
        "reasons": reasons,
        "score_policy": "UNCALIBRATED_DIAGNOSTIC_ONLY",
        "human_alignment": "UNAVAILABLE_WITHOUT_HUMAN_CALIBRATION",
        "award_prediction": "UNAVAILABLE_WITHOUT_HUMAN_CALIBRATION",
        "automatic_cutover": False,
    }


def _atomic_write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--aggregate")
    parser.add_argument("--visual-gate")
    parser.add_argument("--failure-kind", choices=("packet", "infrastructure", "indeterminate"))
    parser.add_argument("--policy-mode", choices=("shadow", "enforce"), default="shadow")
    parser.add_argument("--output", required=True)
    parser.add_argument("--print-decision", action="store_true")
    args = parser.parse_args()
    try:
        aggregate = _read_json(Path(args.aggregate)) if args.aggregate else None
        visual = _read_json(Path(args.visual_gate)) if args.visual_gate else None
        route = route_decision(
            aggregate,
            visual,
            failure_kind=args.failure_kind,
            policy_mode=args.policy_mode,
        )
        _atomic_write(Path(args.output), route)
        if args.print_decision:
            print(route["effective_decision"])
        else:
            print(json.dumps(route, ensure_ascii=False, indent=2))
        return 0
    except (OSError, RoutingError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
