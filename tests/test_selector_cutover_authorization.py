import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from scripts.capability_harness import evaluator_fingerprint, file_sha256
from scripts.hard_gate_calibration import REPORT_SCHEMA as R0A_REPORT_SCHEMA
from scripts.selector_calibration import REPORT_SCHEMA as R0B_REPORT_SCHEMA
from scripts.selector_cutover_authorization import (
    ASSESSMENT_SCHEMA,
    RECEIPT_SCHEMA,
    AuthorizationError,
    main,
    validate_authorization,
)
from scripts.shadow_portfolio import REPORT_SCHEMA as R3_REPORT_SCHEMA


AS_OF = datetime(2026, 8, 6, tzinfo=UTC)


def _evaluator(model):
    return {
        "model": model,
        "backend": "test-backend",
        "prompt_sha256": "a" * 64 if model.startswith("hard") else "b" * 64,
        "schema_sha256": "c" * 64,
    }


def _write(path: Path, value: dict) -> Path:
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def _fixture(tmp_path: Path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    hard = _evaluator("hard-gate-judge-v1")
    selector = _evaluator("selector-judge-v1")
    hard_fp = evaluator_fingerprint(hard)
    selector_fp = evaluator_fingerprint(selector)
    r0a = {
        "schema": R0A_REPORT_SCHEMA,
        "evaluator": hard,
        "evaluator_identity_fingerprint": hard_fp,
        "hard_gate_ready": True,
        "automatic_switch_performed": False,
        "operator_authorization_required": True,
        "claim_limit": "EXACT_RUNTIME_ORACLE_CAPABILITY_AND_REPEATABILITY_ONLY",
    }
    r0b = {
        "schema": R0B_REPORT_SCHEMA,
        "evaluator": selector,
        "evaluator_identity_fingerprint": selector_fp,
        "hard_gate_identity_fingerprint": hard_fp,
        "comparison_ready_human": True,
        "tie_band": 0.2,
        "holdout_hash": "e" * 64,
        "advisory_only": True,
        "automatic_switch_performed": False,
        "operator_authorization_required": True,
        "production_selection_authorized": False,
        "claim_limit": "BLIND_PAIRWISE_SELECTOR_CALIBRATION_ONLY",
    }
    r0a_path = _write(tmp_path / "r0a.json", r0a)
    r0b_path = _write(tmp_path / "r0b.json", r0b)
    r3 = {
        "schema": R3_REPORT_SCHEMA,
        "r0a_report_sha256": file_sha256(r0a_path),
        "selector_report_sha256": file_sha256(r0b_path),
        "hard_gate_identity_fingerprint": hard_fp,
        "selector_identity_fingerprint": selector_fp,
        "tie_band": 0.2,
        "portfolio_ready": True,
        "advisory_only": True,
        "automatic_switch_performed": False,
        "operator_authorization_required": True,
        "production_selection_authorized": False,
        "claim_limit": "SHADOW_PORTFOLIO_RECOMMENDATION_ONLY",
        "gate2_isolated": True,
        "gate2_evaluator_identity_fingerprint": "f" * 64,
        "gate2_isolation_receipt_sha256": "d" * 64,
        "gate2_hidden_fields": [
            "selector_recommendation",
            "candidate_scores",
            "rejected_candidate_identity",
        ],
        "selector_labels_from_gate2": False,
    }
    r3_path = _write(tmp_path / "r3.json", r3)
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "authorization_id": "selector-canary-001",
        "approved_by": "operator@example.test",
        "approved_at": "2026-08-05T00:00:00Z",
        "expires_at": "2026-08-10T00:00:00Z",
        "reason": "Reviewed frozen R0a, R0b, and R3 evidence for limited canary.",
        "revoked": False,
        "canary_only": True,
        "automatic_switch_performed": False,
        "reports": {
            "r0a": {"path": r0a_path.name, "sha256": file_sha256(r0a_path)},
            "r0b": {"path": r0b_path.name, "sha256": file_sha256(r0b_path)},
            "r3": {"path": r3_path.name, "sha256": file_sha256(r3_path)},
        },
        "hard_gate_evaluator": hard,
        "selector_evaluator": selector,
        "scope": {
            "workflow_steps": [3, 5],
            "project_ids": ["canary-project"],
            "problem_types": ["optimization"],
            "maximum_k": 3,
            "budget_policy_sha256": "d" * 64,
            "packet_builder_sha256": "e" * 64,
            "tie_band": 0.2,
        },
    }
    return receipt


def test_valid_receipt_is_assessed_without_changing_routing(tmp_path):
    receipt = _fixture(tmp_path)
    assessment = validate_authorization(receipt, tmp_path, as_of=AS_OF)
    assert assessment["schema"] == ASSESSMENT_SCHEMA
    assert assessment["authorization_valid"] is True
    assert assessment["canary_only"] is True
    assert assessment["automatic_switch_performed"] is False
    assert assessment["route_change_event_required"] is True


def test_expired_or_revoked_receipt_is_rejected(tmp_path):
    receipt = _fixture(tmp_path)
    with pytest.raises(AuthorizationError, match="expired"):
        validate_authorization(
            receipt, tmp_path, as_of=datetime(2026, 8, 11, tzinfo=UTC)
        )
    receipt["revoked"] = True
    with pytest.raises(AuthorizationError, match="unrevoked"):
        validate_authorization(receipt, tmp_path, as_of=AS_OF)


def test_report_hash_and_identity_drift_fail_closed(tmp_path):
    receipt = _fixture(tmp_path)
    receipt["reports"]["r3"]["sha256"] = "0" * 64
    with pytest.raises(AuthorizationError, match="hash-pinned"):
        validate_authorization(receipt, tmp_path, as_of=AS_OF)

    receipt = _fixture(tmp_path / "identity")
    receipt["selector_evaluator"] = _evaluator("different-selector")
    with pytest.raises(AuthorizationError, match="differs from R0b"):
        validate_authorization(receipt, tmp_path / "identity", as_of=AS_OF)


def test_scope_tie_band_and_canary_policy_fail_closed(tmp_path):
    receipt = _fixture(tmp_path)
    receipt["scope"]["tie_band"] = 0.3
    with pytest.raises(AuthorizationError, match="tie_band"):
        validate_authorization(receipt, tmp_path, as_of=AS_OF)

    receipt = _fixture(tmp_path / "canary")
    receipt["canary_only"] = False
    with pytest.raises(AuthorizationError, match="canary_only"):
        validate_authorization(receipt, tmp_path / "canary", as_of=AS_OF)


def test_cli_writes_assessment_for_reproducible_as_of(tmp_path):
    receipt = _fixture(tmp_path)
    receipt_path = _write(tmp_path / "receipt.json", receipt)
    output = tmp_path / "assessment.json"
    assert main(
        [
            str(receipt_path),
            "--json-output",
            str(output),
            "--as-of",
            "2026-08-06T00:00:00Z",
        ]
    ) == 0
    assert json.loads(output.read_text(encoding="utf-8"))["authorization_valid"] is True
