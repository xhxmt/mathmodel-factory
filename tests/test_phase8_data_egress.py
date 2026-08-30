from __future__ import annotations

from copy import deepcopy
from dataclasses import FrozenInstanceError
import json

import pytest

from factory_core.data_egress import (
    DATA_EGRESS_APPROVAL_SCHEMA,
    DATA_EGRESS_REQUEST_SCHEMA,
    DataEgressError,
    data_egress_policy_sha256,
    evaluate_data_egress,
    verify_data_egress_decision,
)


def _request() -> dict[str, object]:
    return {
        "schema_version": DATA_EGRESS_REQUEST_SCHEMA,
        "subject": "project:ordinary-reference-review",
        "provider": "mock-provider",
        "surface": "mock-review-surface",
        "account_scope": "tenant:example/account:test",
        "retention": "ephemeral-session",
        "purpose": "review-reference-evidence",
        "artifacts": [
            {
                "artifact_id": "reference-text-1",
                "sha256": "a" * 64,
                "byte_length": 1200,
                "transfer_form": "canonical-text",
                "classification": "internal",
            },
            {
                "artifact_id": "reference-render-2",
                "sha256": "b" * 64,
                "byte_length": 2200,
                "transfer_form": "rendered",
                "classification": "internal",
            },
        ],
    }


def _approval_for(request: dict[str, object]) -> dict[str, object]:
    staged = evaluate_data_egress(request).staged_manifest
    return {
        "schema_version": DATA_EGRESS_APPROVAL_SCHEMA,
        "approval_id": "approval-ordinary-1",
        "approved": True,
        "staged_manifest_sha256": staged.staged_manifest_sha256,
        "subject": staged.subject,
        "policy_sha256": staged.policy_sha256,
        "purpose": staged.purpose,
        "artifacts": [artifact.approval_binding() for artifact in staged.artifacts],
    }


def _replace(root: object, path: tuple[object, ...], value: object) -> None:
    target = root
    for part in path[:-1]:
        target = target[part]  # type: ignore[index]
    target[path[-1]] = value  # type: ignore[index]


def test_missing_approval_denies_but_preserves_canonical_staged_manifest():
    decision = evaluate_data_egress(_request())

    assert decision.status == "DENIED"
    assert decision.reason_code == "APPROVAL_MISSING"
    assert decision.staged_manifest.state == "STAGED"
    assert decision.staged_manifest.staged_manifest_sha256
    assert decision.approval is None
    assert decision.dispatch_performed is False
    assert verify_data_egress_decision(decision)


def test_exact_approval_authorizes_without_dispatching():
    request = _request()
    decision = evaluate_data_egress(request, _approval_for(request))

    assert decision.status == "AUTHORIZED"
    assert decision.reason_code == "EXACT_APPROVAL_MATCH"
    assert decision.dispatch_performed is False
    assert decision.approval is not None
    assert decision.approval.subject == decision.staged_manifest.subject
    assert verify_data_egress_decision(decision)


def test_request_artifacts_are_canonically_sorted_and_input_is_not_modified():
    request = _request()
    before = deepcopy(request)

    first = evaluate_data_egress(request)
    request["artifacts"].reverse()  # type: ignore[union-attr]
    second = evaluate_data_egress(request)

    assert before["artifacts"] != request["artifacts"]
    assert [item.artifact_id for item in first.staged_manifest.artifacts] == [
        "reference-render-2",
        "reference-text-1",
    ]
    assert first.staged_manifest == second.staged_manifest


def test_serialized_authorized_decision_is_independently_reverified():
    request = _request()
    decision = evaluate_data_egress(request, _approval_for(request))
    serialized = json.loads(json.dumps(decision.as_dict()))

    assert verify_data_egress_decision(serialized)
    assert serialized["decision_sha256"] == decision.decision_sha256


def test_policy_identity_is_stable_and_bound_to_manifest_and_decision():
    first = evaluate_data_egress(_request())
    second = evaluate_data_egress(_request())

    assert data_egress_policy_sha256() == first.policy_sha256
    assert first.policy_sha256 == first.staged_manifest.policy_sha256
    assert first.decision_sha256 == second.decision_sha256


def test_decision_and_nested_staged_manifest_are_frozen():
    decision = evaluate_data_egress(_request())

    with pytest.raises(FrozenInstanceError):
        decision.status = "AUTHORIZED"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        decision.staged_manifest.purpose = "changed"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        (("schema_version",), "data-egress-request-v2", "unsupported.*schema"),
        (("subject",), "", "subject.*non-blank"),
        (("provider",), " ", "provider.*non-blank"),
        (("surface",), None, "surface.*non-blank"),
        (("account_scope",), "\n", "account_scope.*non-blank"),
        (("retention",), 7, "retention.*non-blank"),
        (("purpose",), "", "purpose.*non-blank"),
        (("artifacts", 0, "artifact_id"), "", "artifact_id.*non-blank"),
        (("artifacts", 0, "sha256"), "B" * 64, "lowercase SHA-256"),
        (("artifacts", 0, "byte_length"), True, "non-negative integer"),
        (("artifacts", 0, "byte_length"), -1, "non-negative integer"),
        (("artifacts", 0, "transfer_form"), "provider-native", "unsupported.*form"),
        (("artifacts", 0, "classification"), "secret-ish", "unsupported.*classification"),
    ],
)
def test_common_invalid_request_fact_fails_before_staging(path, value, message):
    request = _request()
    _replace(request, path, value)

    with pytest.raises(DataEgressError, match=message):
        evaluate_data_egress(request)


@pytest.mark.parametrize("artifacts", [None, [], "artifact"])
def test_artifact_collection_must_be_a_nonempty_array(artifacts):
    request = _request()
    request["artifacts"] = artifacts

    with pytest.raises(DataEgressError, match="non-empty array"):
        evaluate_data_egress(request)


def test_duplicate_artifact_id_is_rejected_before_approval():
    request = _request()
    request["artifacts"][0]["artifact_id"] = "reference-render-2"  # type: ignore[index]

    with pytest.raises(DataEgressError, match="artifact_id must be unique"):
        evaluate_data_egress(request)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("staged_manifest_sha256",), "0" * 64),
        (("subject",), "project:different"),
        (("policy_sha256",), "1" * 64),
        (("purpose",), "different-purpose"),
        (("artifacts", 0, "sha256"), "2" * 64),
    ],
)
def test_well_formed_but_nonmatching_approval_is_denied_and_reverifiable(path, value):
    request = _request()
    approval = _approval_for(request)
    _replace(approval, path, value)

    decision = evaluate_data_egress(request, approval)

    assert decision.status == "DENIED"
    assert decision.reason_code == "APPROVAL_BINDING_MISMATCH"
    assert decision.staged_manifest.state == "STAGED"
    assert decision.dispatch_performed is False
    assert verify_data_egress_decision(decision.as_dict())


def test_explicitly_unapproved_receipt_is_denied_without_dispatch():
    request = _request()
    approval = _approval_for(request)
    approval["approved"] = False

    decision = evaluate_data_egress(request, approval)

    assert decision.status == "DENIED"
    assert decision.reason_code == "APPROVAL_NOT_GRANTED"
    assert decision.dispatch_performed is False
    assert verify_data_egress_decision(decision)


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        (("schema_version",), "data-egress-approval-v2", "unsupported.*schema"),
        (("approved",), "yes", "must be a boolean"),
        (("approval_id",), "", "approval_id.*non-blank"),
        (("staged_manifest_sha256",), "Z" * 64, "lowercase SHA-256"),
        (("artifacts", 0, "artifact_id"), "", "artifact_id.*non-blank"),
    ],
)
def test_malformed_approval_fails_validation(path, value, message):
    request = _request()
    approval = _approval_for(request)
    _replace(approval, path, value)

    with pytest.raises(DataEgressError, match=message):
        evaluate_data_egress(request, approval)


def test_approval_artifacts_must_use_manifest_canonical_order():
    request = _request()
    approval = _approval_for(request)
    approval["artifacts"].reverse()  # type: ignore[union-attr]

    with pytest.raises(DataEgressError, match="canonical order"):
        evaluate_data_egress(request, approval)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("status",), "AUTHORIZED"),
        (("reason_code",), "EXACT_APPROVAL_MATCH"),
        (("dispatch_performed",), True),
        (("policy_sha256",), "0" * 64),
        (("request_sha256",), "1" * 64),
        (("staged_manifest", "surface"), "changed-surface"),
        (("decision_sha256",), "2" * 64),
    ],
)
def test_serialized_decision_tampering_fails_reverification(path, value):
    wire = evaluate_data_egress(_request()).as_dict()
    _replace(wire, path, value)

    assert verify_data_egress_decision(wire) is False
