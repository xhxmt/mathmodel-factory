from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

from .domain import InvalidTransition


class HumanDecisionKind(str, Enum):
    SELECTION = "selection"
    APPROVAL = "approval"
    CONSULTATION = "consultation"


@dataclass(frozen=True)
class HumanDecisionRequest:
    request_id: str
    gate: str
    kind: HumanDecisionKind
    action_type: str
    requested_revision: int
    reason: dict[str, Any]
    evidence: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "gate": self.gate,
            "kind": self.kind.value,
            "type": self.action_type,
            "requested_revision": self.requested_revision,
            "reason": self.reason,
            "evidence": list(self.evidence),
            "metadata": self.metadata,
        }


def decision_kind(action_type: str, gate: str | None = None) -> HumanDecisionKind:
    normalized = action_type.lower()
    if "consult" in normalized:
        return HumanDecisionKind.CONSULTATION
    if (
        "approval" in normalized
        or "override" in normalized
        or (gate or "").endswith("_approval")
        or gate in {"content_freeze", "delivery_freeze_override"}
    ):
        return HumanDecisionKind.APPROVAL
    return HumanDecisionKind.SELECTION


def build_decision_request(
    *,
    project_id: str,
    requested_revision: int,
    action: Mapping[str, Any],
    reason: str | Mapping[str, Any],
    evidence: tuple[str, ...] = (),
) -> HumanDecisionRequest:
    action_type = str(action.get("type") or "human_selection")
    gate = str(action.get("gate") or action_type)
    kind = decision_kind(action_type, gate)
    request_id = hashlib.sha256(
        f"{project_id}:{requested_revision}:{gate}:{action_type}".encode("utf-8")
    ).hexdigest()[:24]
    structured_reason = (
        dict(reason)
        if isinstance(reason, Mapping)
        else {"code": f"awaiting_{kind.value}", "message": str(reason), "evidence": list(evidence)}
    )
    return HumanDecisionRequest(
        request_id=request_id,
        gate=gate,
        kind=kind,
        action_type=action_type,
        requested_revision=requested_revision,
        reason=structured_reason,
        evidence=evidence,
        metadata=dict(action.get("metadata") or {}),
    )


def validate_resolution(
    pending_action: Mapping[str, Any], resolution: Mapping[str, Any]
) -> dict[str, Any]:
    """Validate Selection and Approval as distinct domain decisions."""

    pending_gate = str(pending_action.get("gate") or "")
    resolution_gate = str(resolution.get("gate") or pending_gate)
    if pending_gate and resolution_gate != pending_gate:
        raise InvalidTransition(
            f"pending gate {pending_gate} cannot be resolved as {resolution_gate}"
        )
    metadata = pending_action.get("metadata")
    request = metadata.get("human_decision") if isinstance(metadata, Mapping) else None
    if not isinstance(request, Mapping):
        normalized = dict(resolution)
        if resolution_gate:
            normalized["gate"] = resolution_gate
        return normalized
    kind = (
        HumanDecisionKind(str(request.get("kind")))
        if isinstance(request, Mapping) and request.get("kind")
        else decision_kind(str(pending_action.get("type") or ""), pending_gate)
    )
    normalized = dict(resolution)
    normalized["gate"] = resolution_gate
    normalized["kind"] = kind.value
    if kind is HumanDecisionKind.SELECTION:
        selected = (
            normalized.get("selected_option")
            or normalized.get("selected_option_id")
            or normalized.get("selected_primary")
            or normalized.get("selected")
        )
        if selected in {None, ""}:
            raise InvalidTransition("selection decisions require a selected option")
    elif kind is HumanDecisionKind.APPROVAL:
        approved = normalized.get("approved")
        if approved is None:
            selected = str(
                normalized.get("selected_option")
                or normalized.get("selected_option_id")
                or normalized.get("selected")
                or ""
            ).lower()
            if selected.startswith("approve") or selected in {
                "approved",
                "yes",
                "allow",
                "override",
            }:
                approved = True
            elif selected.startswith("reject") or selected in {"rejected", "no", "deny"}:
                approved = False
        if not isinstance(approved, bool):
            raise InvalidTransition("approval decisions require an explicit approved boolean")
        normalized["approved"] = approved
    elif kind is HumanDecisionKind.CONSULTATION:
        answer = normalized.get("answer") or normalized.get("response")
        if not isinstance(answer, str) or not answer.strip():
            raise InvalidTransition("consultation decisions require a nonempty answer")
    if isinstance(request, Mapping):
        normalized["request_id"] = request.get("request_id")
    return normalized
