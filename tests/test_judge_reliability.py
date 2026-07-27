import json
from pathlib import Path

import pytest

from scripts.judge_reliability import (
    INPUT_SCHEMA,
    OUTPUT_SCHEMA,
    ReliabilityError,
    aggregate_reliability,
    main,
    simple_nominal_alpha,
)


def _input(*, math_runs=None, execution_runs=None, paper_runs=None, units=None):
    roles = {}
    if math_runs is not None:
        roles["math"] = {"kind": "hard", "runs": math_runs}
    if execution_runs is not None:
        roles["execution"] = {"kind": "hard", "runs": execution_runs}
    if paper_runs is not None:
        value = {"kind": "paper", "runs": paper_runs}
        if units is not None:
            value["units"] = units
        roles["paper"] = value
    return {
        "schema": INPUT_SCHEMA,
        "packet_identity": {"packet_sha256": "a" * 64},
        "evaluator_identity": {"model": "judge-v1", "backend": "test"},
        "roles": roles,
    }


def _runs(verdicts, *, scores=None, dimensions=None):
    scores = scores or []
    dimensions = dimensions or []
    result = []
    for index, verdict in enumerate(verdicts):
        run = {
            "run_id": f"run-{index + 1}",
            "verdict": verdict,
            "packet_sha256": "a" * 64,
        }
        if index < len(scores):
            run["score"] = scores[index]
        if index < len(dimensions):
            run["dimensions"] = dimensions[index]
        result.append(run)
    return result


def test_hard_role_requires_complete_unanimous_pass():
    report = aggregate_reliability(
        _input(math_runs=_runs(["PASS", "PASS", "PASS"]), execution_runs=_runs(["PASS", "PASS", "PASS"])),
    )

    math = report["roles"]["math"]
    assert report["schema"] == OUTPUT_SCHEMA
    assert math["verdict"] == "PASS"
    assert math["decision_rule"] == "unanimous_pass_with_complete_runs"
    assert math["repeat_reliability"]["pairwise_agreement_rate"] == 1.0
    assert math["repeat_reliability"]["stability"] == "STABLE"
    assert report["overall"]["verdict"] == "PASS"


def test_hard_fail_is_a_veto_even_when_other_runs_pass():
    report = aggregate_reliability(
        _input(math_runs=_runs(["PASS", "FAIL", "PASS"])),
    )

    role = report["roles"]["math"]
    assert role["verdict"] == "FAIL"
    assert role["decision_rule"] == "hard_veto_any_fail"
    assert report["overall"]["verdict"] == "FAIL"
    assert role["repeat_reliability"]["modal_agreement_rate"] == pytest.approx(2 / 3)


def test_hard_conflict_without_fail_is_indeterminate():
    report = aggregate_reliability(_input(math_runs=_runs(["PASS", "INDETERMINATE", "PASS"])))

    role = report["roles"]["math"]
    assert role["verdict"] == "INDETERMINATE"
    assert role["decision_rule"] == "incomplete_or_nonunanimous_hard_runs"


def test_paper_majority_and_score_dispersion_are_diagnostic_only():
    report = aggregate_reliability(
        _input(
            paper_runs=_runs(
                ["PASS", "REVISE", "PASS"],
                scores=[80, 70, 90],
                dimensions=[{"clarity": 40}, {"clarity": 60}, {"clarity": 50}],
            )
        )
    )

    paper = report["roles"]["paper"]
    assert paper["majority_verdict"] == "PASS"
    assert paper["verdict"] == "PASS"
    assert paper["score"]["median"] == 80.0
    assert paper["score"]["range"] == 20.0
    assert paper["score"]["mad"] == 10.0
    assert paper["dimensions"]["clarity"]["median"] == 50.0
    assert paper["score_semantics"] == "UNCALIBRATED_DIAGNOSTIC_ONLY"
    assert report["award_prediction"] == "UNAVAILABLE_WITHOUT_HUMAN_CALIBRATION"


def test_paper_indeterminate_is_not_averaged_away():
    report = aggregate_reliability(
        _input(paper_runs=_runs(["PASS", "INDETERMINATE", "PASS"])),
    )

    paper = report["roles"]["paper"]
    assert paper["majority_verdict"] == "PASS"
    assert paper["verdict"] == "INDETERMINATE"
    assert paper["decision_rule"] == "paper_indeterminate_is_not_averaged_away"


def test_ab_ba_position_consistency_is_reported_without_becoming_a_gate():
    runs = _runs(["PASS", "PASS", "PASS"])
    runs[0].update({"pair_id": "p1", "orientation": "AB", "winner": "A"})
    runs[1].update({"pair_id": "p1", "orientation": "BA", "winner": "B"})
    runs[2].update({"pair_id": "p2", "orientation": "AB", "winner": "A"})
    report = aggregate_reliability(_input(math_runs=runs))
    metric = report["roles"]["math"]["repeat_reliability"]["position_consistency"]
    assert metric["status"] == "UNKNOWN"
    assert metric["reason"] == "INCOMPLETE_AB_BA_PAIRS"

    runs.append({
        "run_id": "run-4",
        "verdict": "PASS",
        "packet_sha256": "a" * 64,
        "pair_id": "p2",
        "orientation": "BA",
        "winner": "B",
    })
    report = aggregate_reliability(_input(math_runs=runs))
    metric = report["roles"]["math"]["repeat_reliability"]["position_consistency"]
    assert metric["status"] == "OK"
    assert metric["consistency_rate"] == 1.0


def test_invalid_runs_are_counted_and_prevent_hard_pass():
    report = aggregate_reliability(
        _input(
            math_runs=[
                {"run_id": "ok", "verdict": "PASS", "packet_sha256": "a" * 64},
                {"run_id": "bad", "packet_sha256": "a" * 64},
                {"run_id": "ok-3", "verdict": "PASS", "packet_sha256": "a" * 64},
            ]
        ),
    )

    role = report["roles"]["math"]
    assert role["runs_requested"] == 3
    assert role["runs_valid"] == 2
    assert role["runs_invalid"] == 1
    assert role["verdict"] == "INDETERMINATE"
    assert role["repeat_reliability"]["stability"] == "INSUFFICIENT"


def test_invalid_run_does_not_erase_hard_fail_veto():
    report = aggregate_reliability(
        _input(
            math_runs=[
                {"run_id": "bad", "packet_sha256": "a" * 64},
                {"run_id": "fail", "verdict": "FAIL", "packet_sha256": "a" * 64},
                {"run_id": "pass", "verdict": "PASS", "packet_sha256": "a" * 64},
            ]
        ),
    )
    assert report["roles"]["math"]["verdict"] == "FAIL"


def test_single_packet_alpha_is_explicitly_unknown():
    report = aggregate_reliability(_input(math_runs=_runs(["PASS", "PASS", "PASS"])))

    alpha = report["roles"]["math"]["repeat_reliability"]["nominal_alpha"]
    assert alpha["value"] is None
    assert alpha["status"] == "UNKNOWN"
    assert alpha["reason"] == "INSUFFICIENT_UNITS"
    assert "single_packet" in alpha["detail"]


def test_simple_nominal_alpha_handles_multiple_units():
    alpha = simple_nominal_alpha(
        [
            {"unit_id": "packet-a", "ratings": {"r1": "PASS", "r2": "PASS", "r3": "PASS"}},
            {"unit_id": "packet-b", "ratings": {"r1": "FAIL", "r2": "FAIL", "r3": "FAIL"}},
        ]
    )

    assert alpha["status"] == "OK"
    assert alpha["value"] == pytest.approx(1.0)


def test_simple_nominal_alpha_reports_degenerate_and_inconsistent_inputs():
    constant = simple_nominal_alpha(
        [
            {"unit_id": "a", "ratings": {"r1": "PASS", "r2": "PASS"}},
            {"unit_id": "b", "ratings": {"r1": "PASS", "r2": "PASS"}},
        ]
    )
    assert constant["status"] == "UNKNOWN"
    assert constant["reason"] == "DEGENERATE_EXPECTED_DISAGREEMENT"

    inconsistent = simple_nominal_alpha(
        [
            {"unit_id": "a", "ratings": {"r1": "PASS", "r2": "PASS"}},
            {"unit_id": "b", "ratings": {"r1": "FAIL", "r3": "FAIL"}},
        ]
    )
    assert inconsistent["status"] == "UNKNOWN"
    assert inconsistent["reason"] == "INCONSISTENT_RATER_SET"


def test_role_alpha_never_uses_fake_unit_ids_from_one_packet():
    runs = _runs(["PASS", "PASS", "PASS", "PASS"])
    for index, run in enumerate(runs):
        run.update({"unit_id": f"fake-{index // 2}", "rater_id": f"r{index % 2}"})
    report = aggregate_reliability(_input(math_runs=runs))
    alpha = report["roles"]["math"]["repeat_reliability"]["nominal_alpha"]
    assert alpha["status"] == "UNKNOWN"
    assert alpha["reason"] == "INSUFFICIENT_UNITS"


def test_alias_route_labels_are_normalised():
    report = aggregate_reliability(
        _input(math_runs=_runs(["REOPEN_MODEL", "REOPEN_REVISION_MODEL", "PASS"])),
    )
    assert report["roles"]["math"]["verdict"] == "FAIL"
    assert report["roles"]["math"]["verdict_counts"] == {"FAIL": 2, "PASS": 1}


def test_missing_roles_are_rejected():
    with pytest.raises(ReliabilityError, match="roles must be"):
        aggregate_reliability(
            {"schema": INPUT_SCHEMA, "packet_identity": {"packet_sha256": "a" * 64}, "roles": {}}
        )


@pytest.mark.parametrize("identity", [None, {}, {"packet_sha256": "x" * 64}, {"packet_sha256": "A" * 64}])
def test_invalid_packet_identity_returns_structured_invalid(identity):
    payload = _input(math_runs=_runs(["PASS", "PASS", "PASS"]))
    payload["packet_identity"] = identity

    report = aggregate_reliability(payload)

    assert report["schema"] == OUTPUT_SCHEMA
    assert report["input_valid"] is False
    assert report["packet_binding"]["status"] == "INVALID"
    assert report["overall"]["verdict"] == "INDETERMINATE"
    assert report["roles"] == {}


def test_mismatched_run_packet_is_invalid_and_cannot_pass():
    runs = _runs(["PASS", "PASS", "PASS"])
    runs[1]["packet_sha256"] = "b" * 64

    report = aggregate_reliability(_input(math_runs=runs))

    assert report["input_valid"] is False
    assert report["packet_binding"]["status"] == "INVALID"
    assert report["roles"]["math"]["verdict"] == "INDETERMINATE"
    assert report["overall"]["verdict"] == "INDETERMINATE"


def test_role_specific_packet_map_is_bound_exactly():
    packet_map = {"math": "1" * 64, "execution": "2" * 64, "paper": "3" * 64}
    payload = {
        "schema": INPUT_SCHEMA,
        "packet_identity": {"packet_fingerprints": packet_map},
        "roles": {
            "math": {
                "kind": "hard",
                "runs": [
                    {"run_id": f"r{index}", "verdict": "PASS", "packet_fingerprints": packet_map}
                    for index in range(3)
                ],
            }
        },
    }

    report = aggregate_reliability(payload)

    assert report["packet_binding"]["identity_form"] == "role_map"
    assert report["roles"]["math"]["verdict"] == "PASS"


def test_repeat_centric_input_preserves_missing_role_as_invalid():
    packet_hash = "a" * 64
    payload = {
        "schema": INPUT_SCHEMA,
        "packet_identity": {"packet_sha256": packet_hash},
        "roles": {"math": {"kind": "hard"}, "execution": {"kind": "hard"}},
        "repeats": [
            {
                "sample_id": "r1",
                "packet_sha256": packet_hash,
                "decisions": {"math": {"verdict": "PASS"}, "execution": {"verdict": "PASS"}},
            },
            {
                "sample_id": "r2",
                "packet_sha256": packet_hash,
                "decisions": {"math": {"verdict": "PASS"}},
            },
            {
                "sample_id": "r3",
                "packet_sha256": packet_hash,
                "decisions": {"math": {"verdict": "PASS"}, "execution": {"verdict": "PASS"}},
            },
        ],
    }

    report = aggregate_reliability(payload)

    assert report["roles"]["math"]["verdict"] == "PASS"
    assert report["roles"]["execution"]["runs_invalid"] == 1
    assert report["roles"]["execution"]["verdict"] == "INDETERMINATE"
    assert report["hard_gate_status"] == "INDETERMINATE"


def test_required_roles_make_missing_execution_fail_closed():
    payload = _input(math_runs=_runs(["PASS", "PASS", "PASS"]))
    payload["required_roles"] = ["math", "execution"]

    report = aggregate_reliability(payload)

    assert report["missing_roles"] == ["execution"]
    assert report["hard_gate_status"] == "INDETERMINATE"
    assert report["overall"]["verdict"] == "INDETERMINATE"


def test_structural_repeat_error_cannot_leave_hard_gate_pass():
    payload = _input(math_runs=_runs(["PASS", "PASS", "PASS"]))
    payload["repeats"] = [{"sample_id": "broken", "packet_sha256": "a" * 64}]

    report = aggregate_reliability(payload)

    assert report["input_valid"] is False
    assert report["hard_gate_status"] == "INDETERMINATE"


def test_role_alpha_requires_explicit_batch_mode():
    payload = _input(
        paper_runs=_runs(["PASS", "PASS", "PASS"]),
        units=[
            {
                "unit_id": "packet-a",
                "packet_identity": {"packet_sha256": "b" * 64},
                "ratings": {"r1": "PASS", "r2": "PASS"},
            },
            {
                "unit_id": "packet-b",
                "packet_identity": {"packet_sha256": "c" * 64},
                    "ratings": {"r1": "REVISE", "r2": "REVISE"},
            },
        ],
    )
    report = aggregate_reliability(payload)
    assert report["roles"]["paper"]["repeat_reliability"]["nominal_alpha"]["reason"] == "SINGLE_PACKET_ALPHA_REQUIRES_BATCH"

    payload["roles"]["paper"]["alpha_mode"] = "batch"
    report = aggregate_reliability(payload)
    assert report["roles"]["paper"]["repeat_reliability"]["nominal_alpha"]["status"] == "OK"


def test_dimension_specs_make_score_sum_contract_explicit():
    payload = _input(
        paper_runs=_runs(
            ["PASS", "PASS", "PASS"],
            scores=[90, 90, 90],
            dimensions=[{"a": 40, "b": 40}] * 3,
        )
    )
    payload["roles"]["paper"]["dimension_specs"] = {"a": 50, "b": 50}
    report = aggregate_reliability(payload)
    assert report["roles"]["paper"]["score_valid_runs"] == 0
    assert report["score_available"] is False


def test_partial_required_dimensions_do_not_produce_recomputed_total():
    payload = _input(
        paper_runs=_runs(
            ["PASS", "PASS", "PASS"],
            scores=[40, 40, 40],
            dimensions=[{"a": 40}] * 3,
        )
    )
    payload["roles"]["paper"]["required_dimensions"] = ["a", "b"]
    report = aggregate_reliability(payload)
    paper = report["roles"]["paper"]
    assert paper["median_recomputed_from_dimensions"] is None
    assert paper["median_total_delta"] is None


def test_position_consistency_maps_ba_back_to_underlying_candidate():
    runs = _runs(["PASS", "PASS"])
    runs[0].update({"pair_id": "p", "orientation": "AB", "winner": "A"})
    runs[1].update({"pair_id": "p", "orientation": "BA", "winner": "B"})
    report = aggregate_reliability(_input(math_runs=runs))
    position = report["roles"]["math"]["repeat_reliability"]["position_consistency"]
    assert position["status"] == "OK"
    assert position["consistency_rate"] == 1.0


def test_cli_writes_versioned_report(tmp_path: Path):
    source = tmp_path / "input.json"
    output = tmp_path / "report.json"
    source.write_text(
        json.dumps(_input(math_runs=_runs(["PASS", "PASS", "PASS"])), ensure_ascii=False),
        encoding="utf-8",
    )

    assert main([str(source), "--output", str(output)]) == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["schema"] == OUTPUT_SCHEMA
    assert report["roles"]["math"]["verdict"] == "PASS"


def test_single_sample_is_complete_diagnostic_but_not_repeatability_pass():
    report = aggregate_reliability(
        _input(
            math_runs=_runs(["PASS"]),
            execution_runs=_runs(["PASS"]),
            paper_runs=_runs(["PASS"], scores=[80], dimensions=[{"clarity": 80}]),
        ),
        min_runs=1,
    )

    assert report["schema"] == OUTPUT_SCHEMA
    assert report["minimum_runs"] == 1
    assert report["minimum_repeatability_runs"] == 2
    assert report["repeatability_scope"] == "SINGLE_SAMPLE_DIAGNOSTIC_ONLY"
    assert report["roles"]["math"]["verdict"] == "INDETERMINATE"
    assert report["roles"]["math"]["repeat_reliability"]["stability"] == "INSUFFICIENT"
    assert report["hard_gate_status"] == "INDETERMINATE"
