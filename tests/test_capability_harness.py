import hashlib
import json
from pathlib import Path

import pytest

from scripts.capability_harness import (
    MANIFEST_SCHEMA,
    OBSERVATION_SCHEMA,
    CapabilityError,
    evaluate_prepared,
    file_sha256,
    main,
    prepare_manifest,
    wilson_interval,
)


def _packet(root: Path, name: str, *, whitespace: bool = False) -> Path:
    packet = root / name
    packet.mkdir()
    context = "speed = 10 m/s\nanswer = 42\n"
    if whitespace:
        context = "speed = 10 m/s   \nanswer = 42\t\n"
    (packet / "context.txt").write_text(context, encoding="utf-8")
    # Preserve deliberately unsorted input bytes for json_reorder_keys.
    (packet / "manifest.json").write_text('{"z": 1, "a": {"value": 2}}\n', encoding="utf-8")
    return packet


def _evaluator(root: Path):
    prompt = root / "runtime_prompt.txt"
    schema = root / "runtime_schema.json"
    prompt.write_text("runtime prompt v1", encoding="utf-8")
    schema.write_text('{"schema": "judge-role-v1"}\n', encoding="utf-8")
    return {
        "model": "judge-model-v1",
        "backend": "test-backend",
        "prompt_path": prompt.name,
        "schema_path": schema.name,
        "prompt_sha256": file_sha256(prompt),
        "schema_sha256": file_sha256(schema),
    }


def _hard_case(source: str = "hard_source"):
    return {
        "id": "hard_unit_error",
        "project_id": "project-hard",
        "problem_id": "problem-hard",
        "mutation_family": "unit_error",
        "role": "math",
        "kind": "hard_defect",
        "split": "test",
        "source_packet": source,
        "mutation": {
            "type": "text_replace",
            "path": "context.txt",
            "old": "10 m/s",
            "new": "10 km/s",
        },
        "oracles": {
            "preconditions": [
                {"type": "text_contains", "path": "context.txt", "value": "10 m/s"}
            ],
            "postconditions": [
                {"type": "text_contains", "path": "context.txt", "value": "10 km/s"},
                {"type": "text_not_contains", "path": "context.txt", "value": "10 m/s"},
            ],
        },
    }


def _neutral_case(source: str = "neutral_source"):
    return {
        "id": "neutral_key_order",
        "project_id": "project-neutral",
        "problem_id": "problem-neutral",
        "mutation_family": "json_key_order",
        "role": "math",
        "kind": "neutral_transform",
        "split": "test",
        "source_packet": source,
        "mutation": {"type": "json_reorder_keys", "path": "manifest.json"},
        "oracles": {
            "preconditions": [{"type": "json_path_equals", "path": "manifest.json", "pointer": "/z", "value": 1}],
            "postconditions": [
                {"type": "json_semantically_equal_to_source", "path": "manifest.json"},
                {"type": "different_from_source", "path": "manifest.json"},
            ],
        },
    }


def _manifest(cases, root: Path):
    return {
        "schema": MANIFEST_SCHEMA,
        "evaluator": _evaluator(root),
        "holdout_axes": ["project_id", "problem_id", "mutation_family"],
        "cases": cases,
    }


def _observation(case, decision, *, baseline=None, grounded=True, trials=None):
    result = {
        "schema": OBSERVATION_SCHEMA,
        "case_id": case["id"],
        "runtime_identity": case["runtime_identity"],
        "decision": decision,
        "findings": [] if decision == "PASS" else [
            {
                "reference": "chunk:1",
                "mutation_family": case["mutation_family"],
                "grounded": grounded,
            }
        ],
        "position_trials": trials or [],
    }
    if baseline is not None:
        result["baseline_decision"] = baseline
    return result


def _write_observation(output: Path, case: dict, value: dict) -> None:
    """Attach a packet-bound grounding receipt to the synthetic observation.

    The production harness intentionally requires every observation to carry a
    pinned receipt.  Keeping this setup here (rather than weakening the
    validator for legacy-shaped test data) makes the fixtures exercise the
    same contract as real judge output.
    """
    packet = output / case["packet_path"]
    manifest_path = packet / "manifest.json"
    context_path = packet / "context.txt"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    context_lines = context_path.read_text(encoding="utf-8").splitlines()
    quote = context_lines[0] if context_lines else "context"
    context_entry = next(
        (item for item in manifest.get("files", []) if item.get("path") == "context.txt"),
        {},
    )
    ref_id = "ref-1"
    ref = {
        "ref_id": ref_id,
        "chunk_id": context_entry.get("chunk_id", "0" * 64),
        "quote": quote,
        "quote_sha256": hashlib.sha256(quote.encode("utf-8")).hexdigest(),
        "resolved_path": "context.txt",
        "line_start": 1,
        "line_end": 1,
        "context_line_start": 1,
        "context_line_end": 1,
    }
    grounded = all(
        finding.get("grounded", True) is True
        for finding in value.get("findings", [])
        if isinstance(finding, dict)
    )
    receipt = {
        "schema_version": "evidence-grounding-v1",
        "role": case["role"],
        "valid": grounded,
        "refs": [ref] if grounded and value.get("findings") else [],
        "errors": [] if grounded else [{"code": "TEST_UNGROUNDED"}],
        "manifest": {
            "path": "manifest.json",
            "sha256": file_sha256(manifest_path),
        },
        "context": {
            "path": "context.txt",
            "sha256": file_sha256(context_path),
        },
    }
    receipt_path = output / "grounding" / f"{case['id']}.json"
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    value["grounding_receipt"] = {
        "path": receipt_path.relative_to(output).as_posix(),
        "sha256": file_sha256(receipt_path),
    }
    for finding in value.get("findings", []):
        if isinstance(finding, dict):
            finding.setdefault("ref_id", ref_id)
    path = output / case["observation_path"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def test_prepare_and_report_exact_runtime_capability(tmp_path):
    _packet(tmp_path, "hard_source")
    _packet(tmp_path, "neutral_source")
    output = tmp_path / "prepared"
    prepared = prepare_manifest(_manifest([_hard_case(), _neutral_case()], tmp_path), tmp_path, output)

    hard, neutral = prepared["cases"]
    assert hard["oracle_validation"]["passed"] is True
    assert hard["packet_sha256"] != hard["source_packet_sha256"]
    assert hard["runtime_identity"]["packet_sha256"] == hard["packet_sha256"]
    assert prepared["claim_limit"] == "ORACLE_BACKED_MUTATION_CAPABILITY_ONLY"

    _write_observation(
        output,
        hard,
        _observation(
            hard,
            "REOPEN_MODEL",
            trials=[
                {"pair_id": "hard-pair", "orientation": "AB", "winner": "A"},
                {"pair_id": "hard-pair", "orientation": "BA", "winner": "B"},
            ],
        ),
    )
    _write_observation(output, neutral, _observation(neutral, "PASS", baseline="PASS"))

    report = evaluate_prepared(prepared, output)

    assert report["metrics"]["sensitivity"]["estimate"] == 1.0
    assert report["metrics"]["specificity"]["estimate"] == 1.0
    assert report["metrics"]["precision"]["estimate"] == 1.0
    assert report["metrics"]["neutral_flip_rate"]["estimate"] == 0.0
    assert report["metrics"]["position_bias_rate"]["estimate"] == 0.0
    assert report["metrics"]["evidence_grounding_rate"]["estimate"] == 1.0
    assert report["metrics"]["indeterminate_rate"]["estimate"] == 0.0
    assert report["metrics"]["false_reopen_rate"]["estimate"] == 0.0
    assert report["capability_matrix"][0]["truth_claim"] == "NONE"
    assert report["award_prediction"] == "UNAVAILABLE_WITHOUT_HUMAN_CALIBRATION"


def test_prepare_rejects_case_without_precondition_oracle(tmp_path):
    _packet(tmp_path, "hard_source")
    case = _hard_case()
    case["oracles"]["preconditions"] = []

    with pytest.raises(CapabilityError, match="at least one oracle"):
        prepare_manifest(_manifest([case], tmp_path), tmp_path, tmp_path / "prepared")


def test_prepare_rejects_stale_prompt_hash(tmp_path):
    _packet(tmp_path, "hard_source")
    manifest = _manifest([_hard_case()], tmp_path)
    manifest["evaluator"]["prompt_sha256"] = "0" * 64

    with pytest.raises(CapabilityError, match="does not match prompt_path"):
        prepare_manifest(manifest, tmp_path, tmp_path / "prepared")


def test_delete_file_mutation_requires_and_proves_missing_evidence(tmp_path):
    packet = _packet(tmp_path, "hard_source")
    (packet / "replay.json").write_text('{"ok": true}\n', encoding="utf-8")
    case = _hard_case()
    case["id"] = "missing-replay"
    case["mutation_family"] = "missing_replay_evidence"
    case["mutation"] = {"type": "delete_file", "path": "replay.json"}
    case["oracles"] = {
        "preconditions": [{"type": "file_exists", "path": "replay.json"}],
        "postconditions": [{"type": "file_missing", "path": "replay.json"}],
    }

    prepared = prepare_manifest(_manifest([case], tmp_path), tmp_path, tmp_path / "prepared")

    assert prepared["cases"][0]["oracle_validation"]["postconditions"][0]["passed"] is True
    assert not (tmp_path / "prepared" / prepared["cases"][0]["packet_path"] / "replay.json").exists()


def test_prepare_rejects_neutral_case_without_equivalence_oracle(tmp_path):
    _packet(tmp_path, "neutral_source")
    case = _neutral_case()
    case["oracles"]["postconditions"] = [
        {"type": "different_from_source", "path": "manifest.json"}
    ]

    with pytest.raises(CapabilityError, match="source-equivalence"):
        prepare_manifest(_manifest([case], tmp_path), tmp_path, tmp_path / "prepared")


def test_prepare_rejects_failed_postcondition(tmp_path):
    _packet(tmp_path, "hard_source")
    case = _hard_case()
    case["oracles"]["postconditions"][0]["value"] = "not-created"

    with pytest.raises(CapabilityError, match="postconditions.*failed"):
        prepare_manifest(_manifest([case], tmp_path), tmp_path, tmp_path / "prepared")


@pytest.mark.parametrize("axis", ["project_id", "problem_id", "mutation_family"])
def test_prepare_rejects_train_test_leakage_on_each_axis(tmp_path, axis):
    _packet(tmp_path, "hard_source")
    _packet(tmp_path, "second_source")
    train = _hard_case()
    train["id"] = "train-case"
    train["split"] = "train"
    test = _hard_case("second_source")
    test["id"] = "test-case"
    for field in ("project_id", "problem_id", "mutation_family"):
        test[field] = f"different-{field}"
    test[axis] = train[axis]

    with pytest.raises(CapabilityError, match=f"leakage on {axis}"):
        prepare_manifest(_manifest([train, test], tmp_path), tmp_path, tmp_path / "prepared")


def test_report_rejects_packet_changed_after_oracle_validation(tmp_path):
    _packet(tmp_path, "hard_source")
    output = tmp_path / "prepared"
    prepared = prepare_manifest(_manifest([_hard_case()], tmp_path), tmp_path, output)
    case = prepared["cases"][0]
    _write_observation(output, case, _observation(case, "REOPEN_MODEL"))
    (output / case["packet_path"] / "context.txt").write_text("tampered", encoding="utf-8")

    with pytest.raises(CapabilityError, match="changed after oracle"):
        evaluate_prepared(prepared, output)


def test_report_rejects_observation_from_different_model_identity(tmp_path):
    _packet(tmp_path, "hard_source")
    output = tmp_path / "prepared"
    prepared = prepare_manifest(_manifest([_hard_case()], tmp_path), tmp_path, output)
    case = prepared["cases"][0]
    observation = _observation(case, "REOPEN_MODEL")
    observation["runtime_identity"] = {**case["runtime_identity"], "model": "other-model"}
    _write_observation(output, case, observation)

    with pytest.raises(CapabilityError, match="runtime_identity does not match"):
        evaluate_prepared(prepared, output)


def test_indeterminate_counts_against_sensitivity_instead_of_being_dropped(tmp_path):
    _packet(tmp_path, "hard_source")
    output = tmp_path / "prepared"
    prepared = prepare_manifest(_manifest([_hard_case()], tmp_path), tmp_path, output)
    case = prepared["cases"][0]
    _write_observation(output, case, _observation(case, "INDETERMINATE"))

    report = evaluate_prepared(prepared, output)

    assert report["metrics"]["sensitivity"]["denominator"] == 1
    assert report["metrics"]["sensitivity"]["estimate"] == 0.0
    assert report["metrics"]["indeterminate_rate"]["estimate"] == 1.0


def test_reopen_for_unrelated_family_is_not_a_true_positive(tmp_path):
    _packet(tmp_path, "hard_source")
    output = tmp_path / "prepared"
    prepared = prepare_manifest(_manifest([_hard_case()], tmp_path), tmp_path, output)
    case = prepared["cases"][0]
    observation = _observation(case, "REOPEN_MODEL")
    observation["findings"][0]["mutation_family"] = "unrelated_problem"
    _write_observation(output, case, observation)

    report = evaluate_prepared(prepared, output)

    assert report["metrics"]["sensitivity"]["estimate"] == 0.0


def test_exact_runtime_fail_verdict_counts_as_hard_defect_detection(tmp_path):
    _packet(tmp_path, "hard_source")
    output = tmp_path / "prepared"
    prepared = prepare_manifest(_manifest([_hard_case()], tmp_path), tmp_path, output)
    case = prepared["cases"][0]
    _write_observation(output, case, _observation(case, "FAIL"))

    report = evaluate_prepared(prepared, output)

    assert report["metrics"]["sensitivity"]["estimate"] == 1.0


def test_failing_neutral_baseline_only_contributes_to_invariance(tmp_path):
    _packet(tmp_path, "neutral_source")
    output = tmp_path / "prepared"
    prepared = prepare_manifest(_manifest([_neutral_case()], tmp_path), tmp_path, output)
    case = prepared["cases"][0]
    observation = _observation(case, "REOPEN_MODEL", baseline="REOPEN_MODEL")
    _write_observation(output, case, observation)

    report = evaluate_prepared(prepared, output)

    assert report["metrics"]["neutral_flip_rate"]["estimate"] == 0.0
    assert report["metrics"]["specificity"]["estimate"] is None
    assert report["metrics"]["false_reopen_rate"]["estimate"] is None


def test_position_bias_uses_normalized_ab_ba_preferences(tmp_path):
    _packet(tmp_path, "hard_source")
    output = tmp_path / "prepared"
    prepared = prepare_manifest(_manifest([_hard_case()], tmp_path), tmp_path, output)
    case = prepared["cases"][0]
    _write_observation(
        output,
        case,
        _observation(
            case,
            "REOPEN_MODEL",
            trials=[
                {"pair_id": "pair", "orientation": "AB", "winner": "A"},
                {"pair_id": "pair", "orientation": "BA", "winner": "A"},
            ],
        ),
    )

    report = evaluate_prepared(prepared, output)

    assert report["metrics"]["position_bias_rate"]["estimate"] == 1.0
    assert report["metrics"]["a_selection_rate"]["estimate"] == 1.0


def test_wilson_interval_does_not_treat_one_success_as_high_confidence():
    interval = wilson_interval(1, 1)

    assert 0.0 < interval["low"] < 0.5
    assert interval["high"] == pytest.approx(1.0)


def test_evaluate_rejects_reopen_without_groundable_finding(tmp_path):
    _packet(tmp_path, "hard_source")
    output = tmp_path / "prepared"
    prepared = prepare_manifest(_manifest([_hard_case()], tmp_path), tmp_path, output)
    case = prepared["cases"][0]
    observation = _observation(case, "PASS")
    observation["decision"] = "REOPEN_MODEL"
    _write_observation(output, case, observation)

    with pytest.raises(CapabilityError, match="needs at least one finding"):
        evaluate_prepared(prepared, output)


def test_prepare_and_report_cli_end_to_end(tmp_path):
    _packet(tmp_path, "hard_source")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(_manifest([_hard_case()], tmp_path)), encoding="utf-8")
    output = tmp_path / "run"

    assert main(["prepare", str(manifest_path), "--output-dir", str(output)]) == 0
    prepared_path = output / "prepared_manifest.json"
    prepared = json.loads(prepared_path.read_text(encoding="utf-8"))
    case = prepared["cases"][0]
    _write_observation(output, case, _observation(case, "REOPEN_MODEL"))
    report_path = output / "report.json"

    assert main(["report", str(prepared_path), "--json-output", str(report_path)]) == 0
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["schema"] == "judge-capability-report-v1"
    assert report["metrics"]["sensitivity"]["estimate"] == 1.0
