"""Typed solver-policy commands; no provider dispatch or legacy state writes."""
from __future__ import annotations

import json

from .canonical import canonical_bytes, canonical_sha256

POLICY_SCHEMA = "authority-solver-policy-v1"
POLICY_COMMAND = "CONFIGURE_SOLVER_POLICY"
POLICY_EVENT = "SOLVER_POLICY_CONFIGURED"
POLICY_TOPIC = "authority.solver-policy.changed"


def normalize_policy(mode, threshold_seconds, allowed_runtimes):
    if type(mode) is not str or mode not in {"local", "cloud", "auto"}:
        raise ValueError("unsupported solver mode")
    if type(threshold_seconds) is not int or not 1 <= threshold_seconds <= 86400:
        raise ValueError("solver threshold must be an integer between 1 and 86400 seconds")
    if not isinstance(allowed_runtimes, (list, tuple)) or not allowed_runtimes:
        raise ValueError("at least one solver runtime is required")
    if any(type(value) is not str or not value or value != value.strip() for value in allowed_runtimes):
        raise ValueError("solver runtimes must be nonempty strings")
    return {"schema": POLICY_SCHEMA, "mode": mode, "threshold_seconds": threshold_seconds,
            "allowed_runtimes": sorted(set(allowed_runtimes))}


def decode_policy(raw):
    if type(raw) is not str:
        raise ValueError("solver policy must contain canonical JSON")
    value = json.loads(raw)
    if type(value) is not dict or set(value) != {"schema", "mode", "threshold_seconds", "allowed_runtimes"}:
        raise ValueError("solver policy fields differ")
    normalized = normalize_policy(value["mode"], value["threshold_seconds"], value["allowed_runtimes"])
    if value != normalized or canonical_bytes(value).decode() != raw:
        raise ValueError("solver policy is not canonical")
    return value


def validate_policy_bundle(command, event, receipt, outbox):
    """Bind the actual configuration to all four immutable envelopes."""
    from .authority_envelopes import EnvelopeFieldV1
    from .command_envelope import NoEntityScopeV1, NoSubjectScopeV1, PayloadBindingV1

    if command.command_type.value != POLICY_COMMAND or type(command.payload_binding) is not PayloadBindingV1:
        raise ValueError("solver policy command binding differs")
    if type(command.entity_scope) is not NoEntityScopeV1 or type(command.subject_scope) is not NoSubjectScopeV1:
        raise ValueError("solver policy requires project scope")
    if len(event.fields) != 1 or event.fields[0].key != "policy_json":
        raise ValueError("solver policy event fields differ")
    policy = decode_policy(event.fields[0].value)
    digest = canonical_sha256(policy)
    if command.payload_binding != PayloadBindingV1(POLICY_SCHEMA, digest):
        raise ValueError("solver policy payload identity differs")
    if event.event_type != POLICY_EVENT or receipt.outcome != "RECORDED" or outbox.topic != POLICY_TOPIC:
        raise ValueError("solver policy companion type differs")
    if receipt.fields != (EnvelopeFieldV1("policy_sha256", digest),):
        raise ValueError("solver policy receipt identity differs")
    if outbox.fields != (EnvelopeFieldV1("policy_sha256", digest), EnvelopeFieldV1("receipt_id", receipt.receipt_id)):
        raise ValueError("solver policy notification identity differs")
    return policy
