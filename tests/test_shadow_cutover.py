import json
from pathlib import Path

import pytest

from scripts.capability_harness import (
    REPORT_SCHEMA,
    CapabilityError,
    binomial_metric,
    evaluator_fingerprint,
    file_sha256,
    tree_sha256,
)
from scripts.hard_gate_calibration import REPORT_SCHEMA as HARD_GATE_REPORT_SCHEMA
from scripts.shadow_cutover import LEGACY_SHADOW_SCHEMA, SHADOW_SCHEMA, evaluate_shadow, main


def _evaluator():
    return {
        "model": "new-judge-v1",
        "backend": "test-backend",
        "prompt_sha256": "a" * 64,
        "schema_sha256": "b" * 64,
    }


def _good_metrics(n=100):
    return {
        "sensitivity": binomial_metric(n, n),
        "specificity": binomial_metric(n, n),
        "precision": binomial_metric(n, n),
        "neutral_flip_rate": binomial_metric(0, n),
        "position_bias_rate": binomial_metric(0, n),
        "a_selection_rate": binomial_metric(n // 2, n),
        "evidence_grounding_rate": binomial_metric(n, n),
        "indeterminate_rate": binomial_metric(0, n),
        "false_reopen_rate": binomial_metric(0, n),
    }


def _capability_report(metrics=None, test_cases=100):
    identity = _evaluator()
    fingerprint = evaluator_fingerprint(identity)
    return {
        "schema": REPORT_SCHEMA,
        "evaluator": identity,
        "evaluator_identity_fingerprint": fingerprint,
        "claim_limit": "ORACLE_BACKED_MUTATION_CAPABILITY_ONLY",
        "capability_matrix": [
            {
                "role": "math",
                "evaluator_identity_fingerprint": fingerprint,
                "scope": {
                    "project_ids": ["calibration-project"],
                    "problem_ids": ["calibration-problem"],
                    "mutation_families": ["unit_error", "neutral_whitespace"],
                    "packet_sha256": ["c" * 64],
                },
                "test_cases": test_cases,
                "metrics": metrics or _good_metrics(),
                "permitted_use": "ROLE_ROUTING_AND_SHADOW_ELIGIBILITY_ONLY",
                "truth_claim": "NONE",
            }
        ],
    }


def _thresholds():
    return {
        "minimum_test_cases": 50,
        "minimum_shadow_cases": 50,
        "minimum_shadow_projects": 3,
        "capability": {
            "sensitivity": 0.95,
            "specificity": 0.95,
            "precision": 0.95,
            "evidence_grounding_rate": 0.95,
            "neutral_flip_rate": 0.05,
            "position_bias_rate": 0.05,
            "indeterminate_rate": 0.05,
            "false_reopen_rate": 0.05,
        },
        "shadow": {
            "agreement_rate_min": 0.95,
            "new_indeterminate_rate_max": 0.05,
            "relaxation_rate_max": 0.05,
        },
    }


def _write_packet(root: Path) -> Path:
    packet = root / "shadow-packet"
    packet.mkdir()
    (packet / "context.txt").write_text("runtime packet", encoding="utf-8")
    return packet


def _write_capability(root: Path, report: dict) -> Path:
    path = root / "capability.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    return path


def _write_hard_gate_calibration(root: Path, capability_path: Path, *, ready=True) -> Path:
    identity = _evaluator()
    fingerprint = evaluator_fingerprint(identity)
    path = root / "hard-gate-calibration.json"
    path.write_text(
        json.dumps(
            {
                "schema": HARD_GATE_REPORT_SCHEMA,
                "capability_report_sha256": file_sha256(capability_path),
                "evaluator": identity,
                "evaluator_identity_fingerprint": fingerprint,
                "required_roles": ["math", "execution"],
                "thresholds": {
                    "minimum_test_cases": _thresholds()["minimum_test_cases"],
                    "capability": _thresholds()["capability"],
                },
                "roles": {
                    "math": {"ready": ready},
                    "execution": {"ready": ready},
                },
                "hard_gate_ready": ready,
                "automatic_switch_performed": False,
                "operator_authorization_required": True,
                "claim_limit": "EXACT_RUNTIME_ORACLE_CAPABILITY_AND_REPEATABILITY_ONLY",
            }
        ),
        encoding="utf-8",
    )
    return path


def _manifest(root: Path, capability_path: Path, *, n=100, legacy="PASS", new="PASS"):
    identity = _evaluator()
    packet_hash = tree_sha256(root / "shadow-packet")
    hard_gate_path = _write_hard_gate_calibration(root, capability_path)
    return {
        "schema": SHADOW_SCHEMA,
        "capability_report_sha256": file_sha256(capability_path),
        "hard_gate_calibration_report_path": hard_gate_path.name,
        "hard_gate_calibration_report_sha256": file_sha256(hard_gate_path),
        "target_route": {
            "role": "math",
            "evaluator": identity,
            "evaluator_identity_fingerprint": evaluator_fingerprint(identity),
        },
        "disjoint_from_capability_by": ["project_id"],
        "thresholds": _thresholds(),
        "cases": [
            {
                "id": f"shadow-{index}",
                "project_id": f"shadow-project-{index % 5}",
                "problem_id": "shadow-problem",
                "role": "math",
                "packet_path": "shadow-packet",
                "new_runtime_identity": {**identity, "packet_sha256": packet_hash},
                "legacy_decision": legacy,
                "new_decision": new,
            }
            for index in range(n)
        ],
    }


def test_shadow_can_recommend_cutover_but_never_switches_automatically(tmp_path):
    _write_packet(tmp_path)
    capability_path = _write_capability(tmp_path, _capability_report())
    manifest = _manifest(tmp_path, capability_path)

    report = evaluate_shadow(manifest, tmp_path, _capability_report(), capability_path)

    assert report["cutover_ready"] is True
    assert report["advisory_only"] is True
    assert report["automatic_switch_performed"] is False
    assert report["operator_authorization_required"] is True
    assert report["shadow_metrics"]["agreement_rate"]["estimate"] == 1.0
    assert report["claim_limit"] == "SHADOW_AGREEMENT_AND_ORACLE_CAPABILITY_NOT_HUMAN_TRUTH"


def test_legacy_v1_is_diagnostic_only_and_cannot_recommend_cutover(tmp_path):
    _write_packet(tmp_path)
    capability = _capability_report()
    capability_path = _write_capability(tmp_path, capability)
    manifest = _manifest(tmp_path, capability_path)
    manifest["schema"] = LEGACY_SHADOW_SCHEMA
    manifest.pop("hard_gate_calibration_report_path")
    manifest.pop("hard_gate_calibration_report_sha256")

    report = evaluate_shadow(manifest, tmp_path, capability, capability_path)

    assert report["cutover_ready"] is False
    assert report["legacy_capability_only"] is True
    failed = {check["name"] for check in report["threshold_checks"] if not check["passed"]}
    assert "r0a_hard_gate_calibration" in failed


def test_shadow_v2_requires_ready_hash_bound_r0a_report(tmp_path):
    _write_packet(tmp_path)
    capability = _capability_report()
    capability_path = _write_capability(tmp_path, capability)
    manifest = _manifest(tmp_path, capability_path)
    hard_gate_path = tmp_path / manifest["hard_gate_calibration_report_path"]
    hard_gate = json.loads(hard_gate_path.read_text(encoding="utf-8"))
    hard_gate["hard_gate_ready"] = False
    hard_gate["roles"]["math"]["ready"] = False
    hard_gate_path.write_text(json.dumps(hard_gate), encoding="utf-8")
    manifest["hard_gate_calibration_report_sha256"] = file_sha256(hard_gate_path)

    report = evaluate_shadow(manifest, tmp_path, capability, capability_path)

    assert report["cutover_ready"] is False
    failed = {check["name"] for check in report["threshold_checks"] if not check["passed"]}
    assert "r0a_hard_gate_calibration" in failed

    manifest["hard_gate_calibration_report_sha256"] = "0" * 64
    with pytest.raises(CapabilityError, match="hard-gate calibration report is not pinned"):
        evaluate_shadow(manifest, tmp_path, capability, capability_path)


def test_capability_wilson_lower_bound_blocks_small_or_weak_sample(tmp_path):
    _write_packet(tmp_path)
    weak = _capability_report(metrics=_good_metrics(1), test_cases=1)
    capability_path = _write_capability(tmp_path, weak)
    manifest = _manifest(tmp_path, capability_path)

    report = evaluate_shadow(manifest, tmp_path, weak, capability_path)

    assert report["cutover_ready"] is False
    failed = {check["name"] for check in report["threshold_checks"] if not check["passed"]}
    assert "sensitivity" in failed
    assert "minimum_test_cases" in failed


def test_shadow_wilson_upper_bound_blocks_small_sample_even_with_zero_errors(tmp_path):
    _write_packet(tmp_path)
    capability = _capability_report()
    capability_path = _write_capability(tmp_path, capability)
    manifest = _manifest(tmp_path, capability_path, n=1)
    manifest["thresholds"]["minimum_shadow_cases"] = 1

    report = evaluate_shadow(manifest, tmp_path, capability, capability_path)

    assert report["cutover_ready"] is False
    failed = {check["name"] for check in report["threshold_checks"] if not check["passed"]}
    assert "new_indeterminate_rate_max" in failed
    assert "relaxation_rate_max" in failed


def test_shadow_rejects_unpinned_capability_report(tmp_path):
    _write_packet(tmp_path)
    capability = _capability_report()
    capability_path = _write_capability(tmp_path, capability)
    manifest = _manifest(tmp_path, capability_path)
    manifest["capability_report_sha256"] = "0" * 64

    with pytest.raises(CapabilityError, match="not pinned"):
        evaluate_shadow(manifest, tmp_path, capability, capability_path)


def test_shadow_rejects_packet_identity_mismatch(tmp_path):
    _write_packet(tmp_path)
    capability = _capability_report()
    capability_path = _write_capability(tmp_path, capability)
    manifest = _manifest(tmp_path, capability_path)
    manifest["cases"][0]["new_runtime_identity"]["packet_sha256"] = "d" * 64

    with pytest.raises(CapabilityError, match="runtime identity or packet hash mismatch"):
        evaluate_shadow(manifest, tmp_path, capability, capability_path)


def test_shadow_rejects_capability_data_reuse(tmp_path):
    _write_packet(tmp_path)
    capability = _capability_report()
    capability_path = _write_capability(tmp_path, capability)
    manifest = _manifest(tmp_path, capability_path)
    manifest["cases"][0]["project_id"] = "calibration-project"

    with pytest.raises(CapabilityError, match="leakage on project_id"):
        evaluate_shadow(manifest, tmp_path, capability, capability_path)


def test_shadow_rejects_missing_core_threshold_instead_of_defaulting_to_ready(tmp_path):
    _write_packet(tmp_path)
    capability = _capability_report()
    capability_path = _write_capability(tmp_path, capability)
    manifest = _manifest(tmp_path, capability_path)
    del manifest["thresholds"]["capability"]["precision"]

    with pytest.raises(CapabilityError, match="must configure exactly"):
        evaluate_shadow(manifest, tmp_path, capability, capability_path)


def test_shadow_disagreement_is_reported_without_being_called_truth(tmp_path):
    _write_packet(tmp_path)
    capability = _capability_report()
    capability_path = _write_capability(tmp_path, capability)
    manifest = _manifest(tmp_path, capability_path, legacy="REOPEN_MODEL", new="PASS")

    report = evaluate_shadow(manifest, tmp_path, capability, capability_path)

    assert report["cutover_ready"] is False
    assert report["shadow_metrics"]["relaxation_rate"]["estimate"] == 1.0
    assert report["award_prediction"] == "UNAVAILABLE_WITHOUT_HUMAN_CALIBRATION"


def test_shadow_compares_routing_actions_without_label_alias_noise(tmp_path):
    _write_packet(tmp_path)
    capability = _capability_report()
    capability_path = _write_capability(tmp_path, capability)
    manifest = _manifest(
        tmp_path,
        capability_path,
        legacy="FAIL",
        new="REOPEN_REVISION_MODEL",
    )

    report = evaluate_shadow(manifest, tmp_path, capability, capability_path)

    assert report["shadow_metrics"]["agreement_rate"]["estimate"] == 1.0
    assert report["shadow_metrics"]["raw_label_agreement_rate"]["estimate"] == 0.0
    assert report["shadow_cases"][0]["legacy_action"] == "BLOCK_MODEL"


def test_shadow_cli_require_ready_uses_exit_three_without_switching(tmp_path):
    _write_packet(tmp_path)
    capability = _capability_report()
    capability_path = _write_capability(tmp_path, capability)
    manifest = _manifest(tmp_path, capability_path, n=1)
    manifest["thresholds"]["minimum_shadow_cases"] = 1
    manifest_path = tmp_path / "shadow.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    output = tmp_path / "shadow-report.json"

    assert main(
        [
            str(manifest_path),
            "--capability-report",
            str(capability_path),
            "--json-output",
            str(output),
            "--require-ready",
        ]
    ) == 3
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["cutover_ready"] is False
    assert report["automatic_switch_performed"] is False
