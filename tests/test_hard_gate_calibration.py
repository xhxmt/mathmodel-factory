import json
from pathlib import Path

import pytest

from scripts.capability_harness import (
    PREPARED_SCHEMA,
    REPORT_SCHEMA as CAPABILITY_REPORT_SCHEMA,
    binomial_metric,
    canonical_hash,
    evaluator_fingerprint,
    file_sha256,
    runtime_fingerprint,
    tree_sha256,
)
from scripts.hard_gate_calibration import (
    MANIFEST_SCHEMA,
    REPORT_SCHEMA,
    evaluate_hard_gate_calibration,
    main,
)
from scripts.judge_reliability import INPUT_SCHEMA


def _evaluator() -> dict[str, str]:
    return {
        "model": "hard-gate-judge-v1",
        "backend": "test-backend",
        "prompt_sha256": "a" * 64,
        "schema_sha256": "b" * 64,
    }


def _good_metrics(n: int = 100) -> dict:
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


def _thresholds() -> dict:
    return {
        "minimum_test_cases": 2,
        "capability": {
            "sensitivity": 0.90,
            "specificity": 0.90,
            "precision": 0.90,
            "evidence_grounding_rate": 0.90,
            "neutral_flip_rate": 0.10,
            "position_bias_rate": 0.10,
            "indeterminate_rate": 0.10,
            "false_reopen_rate": 0.10,
        },
    }


def _write_json(path: Path, value: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return path


def _fixture(tmp_path: Path, *, verdicts: dict[str, list[str]] | None = None):
    evaluator = _evaluator()
    fingerprint = evaluator_fingerprint(evaluator)
    verdicts = verdicts or {}
    cases = []
    reliability_cases = []
    observation_receipts = []
    for role in ("math", "execution"):
        for kind, expected in (("hard_defect", "FAIL"), ("neutral_transform", "PASS")):
            case_id = f"{role}-{kind}"
            packet = tmp_path / "prepared" / "cases" / case_id / "packet"
            packet.mkdir(parents=True)
            (packet / "context.txt").write_text(case_id, encoding="utf-8")
            packet_hash = tree_sha256(packet)
            runtime = {**evaluator, "packet_sha256": packet_hash}
            condition = runtime_fingerprint(runtime)
            observation = {
                "schema": "judge-capability-observation-v1",
                "case_id": case_id,
                "runtime_identity": runtime,
                "decision": expected,
            }
            observation_path = _write_json(
                tmp_path / "prepared" / "observations" / f"{case_id}.json",
                observation,
            )
            cases.append(
                {
                    "id": case_id,
                    "project_id": f"project-{case_id}",
                    "problem_id": f"problem-{case_id}",
                    "mutation_family": f"mutation-{case_id}",
                    "role": role,
                    "kind": kind,
                    "split": "test",
                    "packet_path": str(packet.relative_to(tmp_path / "prepared")),
                    "packet_sha256": packet_hash,
                    "observation_path": str(observation_path.relative_to(tmp_path / "prepared")),
                    "runtime_identity": runtime,
                    "runtime_identity_fingerprint": condition,
                    "oracle_validation": {"passed": True},
                }
            )
            observation_receipts.append(
                {
                    "case_id": case_id,
                    "observation_sha256": file_sha256(observation_path),
                    "runtime_identity_fingerprint": condition,
                }
            )
            case_verdicts = verdicts.get(case_id, [expected] * 5)
            reliability = {
                "schema": INPUT_SCHEMA,
                "packet_identity": {
                    "packet_sha256": packet_hash,
                    "condition_fingerprint": condition,
                },
                "evaluator_identity": evaluator,
                "required_roles": [role],
                "roles": {
                    role: {
                        "kind": "hard",
                        "runs": [
                            {
                                "run_id": f"run-{index + 1}",
                                "verdict": verdict,
                                "packet_sha256": packet_hash,
                                "condition_sha256": condition,
                            }
                            for index, verdict in enumerate(case_verdicts)
                        ],
                    }
                },
            }
            reliability_path = _write_json(
                tmp_path / "reliability" / f"{case_id}.json", reliability
            )
            reliability_cases.append(
                {
                    "case_id": case_id,
                    "input_path": str(reliability_path.relative_to(tmp_path)),
                    "input_sha256": file_sha256(reliability_path),
                }
            )

    prepared = {
        "schema": PREPARED_SCHEMA,
        "evaluator": evaluator,
        "evaluator_identity_fingerprint": fingerprint,
        "holdout_audit": {"passed": True},
        "cases": cases,
        "claim_limit": "ORACLE_BACKED_MUTATION_CAPABILITY_ONLY",
    }
    prepared_path = _write_json(tmp_path / "prepared" / "prepared_manifest.json", prepared)
    rows = []
    for role in ("math", "execution"):
        role_cases = [case for case in cases if case["role"] == role]
        rows.append(
            {
                "role": role,
                "evaluator_identity_fingerprint": fingerprint,
                "scope": {
                    "project_ids": sorted(case["project_id"] for case in role_cases),
                    "problem_ids": sorted(case["problem_id"] for case in role_cases),
                    "mutation_families": sorted(case["mutation_family"] for case in role_cases),
                    "packet_sha256": sorted(case["packet_sha256"] for case in role_cases),
                },
                "test_cases": len(role_cases),
                "metrics": _good_metrics(),
                "permitted_use": "ROLE_ROUTING_AND_SHADOW_ELIGIBILITY_ONLY",
                "truth_claim": "NONE",
            }
        )
    capability = {
        "schema": CAPABILITY_REPORT_SCHEMA,
        "prepared_manifest_sha256": canonical_hash(prepared),
        "evaluator": evaluator,
        "evaluator_identity_fingerprint": fingerprint,
        "holdout_audit": {"passed": True},
        "capability_matrix": rows,
        "observation_receipts": observation_receipts,
        "claim_limit": "ORACLE_BACKED_MUTATION_CAPABILITY_ONLY",
    }
    capability_path = _write_json(tmp_path / "capability.json", capability)
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "prepared_manifest_sha256": file_sha256(prepared_path),
        "capability_report_sha256": file_sha256(capability_path),
        "required_roles": ["math", "execution"],
        "minimum_reliability_runs": 5,
        "thresholds": _thresholds(),
        "reliability_cases": reliability_cases,
    }
    return manifest, prepared, prepared_path, capability, capability_path


def _evaluate(tmp_path: Path, values):
    manifest, prepared, prepared_path, capability, capability_path = values
    return evaluate_hard_gate_calibration(
        manifest,
        tmp_path,
        prepared,
        prepared_path,
        capability,
        capability_path,
    )


def test_math_and_execution_hard_and_neutral_cases_can_be_ready(tmp_path):
    values = _fixture(tmp_path)

    report = _evaluate(tmp_path, values)

    assert report["schema"] == REPORT_SCHEMA
    assert report["hard_gate_ready"] is True
    assert report["required_roles"] == ["math", "execution"]
    assert all(value["ready"] for value in report["roles"].values())
    assert report["automatic_switch_performed"] is False
    assert report["operator_authorization_required"] is True
    assert report["claim_limit"] == "EXACT_RUNTIME_ORACLE_CAPABILITY_AND_REPEATABILITY_ONLY"


def test_missing_required_role_or_reliability_case_is_rejected(tmp_path):
    values = _fixture(tmp_path)
    values[0]["required_roles"] = ["math"]
    with pytest.raises(ValueError, match="exactly math and execution"):
        _evaluate(tmp_path, values)

    values = _fixture(tmp_path / "missing-case")
    values[0]["reliability_cases"].pop()
    with pytest.raises(ValueError, match="exactly cover"):
        _evaluate(tmp_path / "missing-case", values)


def test_unpinned_inputs_and_runtime_identity_mismatch_are_rejected(tmp_path):
    values = _fixture(tmp_path)
    values[0]["capability_report_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="capability report is not pinned"):
        _evaluate(tmp_path, values)

    mismatch_root = tmp_path / "mismatch"
    values = _fixture(mismatch_root)
    reliability_ref = values[0]["reliability_cases"][0]
    reliability_path = mismatch_root / reliability_ref["input_path"]
    reliability = json.loads(reliability_path.read_text(encoding="utf-8"))
    reliability["packet_identity"]["condition_fingerprint"] = "f" * 64
    _write_json(reliability_path, reliability)
    reliability_ref["input_sha256"] = file_sha256(reliability_path)
    with pytest.raises(ValueError, match="exact runtime identity"):
        _evaluate(mismatch_root, values)


def test_r0a_requires_five_complete_valid_repeats(tmp_path):
    values = _fixture(tmp_path)
    values[0]["minimum_reliability_runs"] = 4
    with pytest.raises(ValueError, match="at least 5"):
        _evaluate(tmp_path, values)

    invalid_root = tmp_path / "invalid-run"
    values = _fixture(invalid_root)
    reliability_ref = next(
        item
        for item in values[0]["reliability_cases"]
        if item["case_id"] == "execution-hard_defect"
    )
    reliability_path = invalid_root / reliability_ref["input_path"]
    reliability = json.loads(reliability_path.read_text(encoding="utf-8"))
    del reliability["roles"]["execution"]["runs"][0]["verdict"]
    _write_json(reliability_path, reliability)
    reliability_ref["input_sha256"] = file_sha256(reliability_path)

    report = _evaluate(invalid_root, values)

    case = next(
        item for item in report["cases"] if item["case_id"] == "execution-hard_defect"
    )
    assert case["checks"]["complete_runs"] is False
    assert case["ready"] is False
    assert report["hard_gate_ready"] is False


def test_unstable_hard_detection_is_not_ready_even_with_fail_veto(tmp_path):
    values = _fixture(
        tmp_path,
        verdicts={"math-hard_defect": ["PASS", "PASS", "FAIL", "PASS", "PASS"]},
    )

    report = _evaluate(tmp_path, values)

    case = next(item for item in report["cases"] if item["case_id"] == "math-hard_defect")
    assert case["aggregate_verdict"] == "FAIL"
    assert case["stability"] == "UNSTABLE"
    assert case["ready"] is False
    assert report["hard_gate_ready"] is False


def test_capability_observation_and_repeat_aggregate_must_agree(tmp_path):
    values = _fixture(tmp_path)
    prepared = values[1]
    case = next(item for item in prepared["cases"] if item["id"] == "math-hard_defect")
    observation_path = values[2].parent / case["observation_path"]
    observation = json.loads(observation_path.read_text(encoding="utf-8"))
    observation["decision"] = "PASS"
    _write_json(observation_path, observation)
    receipt = next(
        item for item in values[3]["observation_receipts"] if item["case_id"] == case["id"]
    )
    receipt["observation_sha256"] = file_sha256(observation_path)
    _write_json(values[4], values[3])
    values[0]["capability_report_sha256"] = file_sha256(values[4])

    report = _evaluate(tmp_path, values)

    result = next(item for item in report["cases"] if item["case_id"] == case["id"])
    assert result["observation_aggregate_consistent"] is False
    assert result["ready"] is False
    assert report["hard_gate_ready"] is False


def test_cli_require_ready_returns_three_without_authorizing_switch(tmp_path):
    values = _fixture(
        tmp_path,
        verdicts={
            "execution-neutral_transform": ["PASS", "PASS", "FAIL", "PASS", "PASS"]
        },
    )
    manifest_path = _write_json(tmp_path / "r0a.json", values[0])
    output = tmp_path / "r0a-report.json"

    assert main(
        [
            str(manifest_path),
            "--prepared-manifest",
            str(values[2]),
            "--capability-report",
            str(values[4]),
            "--json-output",
            str(output),
            "--require-ready",
        ]
    ) == 3
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["hard_gate_ready"] is False
    assert report["automatic_switch_performed"] is False
