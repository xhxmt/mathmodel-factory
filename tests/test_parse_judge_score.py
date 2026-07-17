import json
from pathlib import Path

from scripts.aggregate_judges import aggregate_outputs, write_aggregate_report
from scripts.parse_judge_score import aggregate, parse_file


DIMENSIONS = {
    "model_presentation": 18,
    "solution_narrative": 18,
    "innovation": 17,
    "writing_clarity": 14,
    "result_persuasiveness": 14,
    "sensitivity_limitations": 9,
}


def _hard(path: Path, role: str, verdict: str = "PASS", fatal: int = 0) -> Path:
    severity = "fatal" if verdict == "FAIL" else "support"
    evidence = [{
        "claim": f"{role} claim",
        "location": "context.txt:1",
        "finding": "specific finding",
        "severity": severity,
    } for _ in range(fatal if verdict == "FAIL" else 1)]
    payload = {
        "schema_version": "judge-role-v1",
        "role": role,
        "verdict": verdict,
        "fatal_flaws": fatal,
        "evidence": evidence,
        "limitations": [],
        "conclusion": "audited conclusion",
    }
    path.write_text(f"VERDICT: {verdict}\n{json.dumps(payload)}\n", encoding="utf-8")
    return path


def _paper(path: Path) -> Path:
    payload = {
        "schema_version": "judge-role-v1",
        "role": "paper",
        "verdict": "PASS",
        "dimensions": {
            key: {
                "score": score,
                "evidence": [{"location": f"paper:{key}", "finding": "evidence"}],
            }
            for key, score in DIMENSIONS.items()
        },
        "overall_score": sum(DIMENSIONS.values()),
        "limitations": [],
        "recommendations": ["first", "second", "third"],
        "conclusion": "paper conclusion",
    }
    path.write_text(f"VERDICT: PASS\n{json.dumps(payload)}\n", encoding="utf-8")
    return path


def _incomplete_manifest(path: Path, role: str) -> Path:
    path.write_text(json.dumps({
        "role": role,
        "files": [{
            "path": "demo_paper.tex",
            "status": "omitted",
            "reason": "context_byte_limit",
        }],
        "completeness": {
            "contract_version": "judge-packet-completeness-v1",
            "status": "INCOMPLETE",
            "eligible": False,
            "requirements": [{
                "id": "final_paper",
                "description": "final paper",
                "required_status": "included",
                "paths": ["demo_paper.tex"],
                "satisfied_paths": [],
                "satisfied": False,
                "failure_reason": "required_artifact_not_fully_included",
            }],
            "limitations": [{
                "path": "demo_paper.tex",
                "status": "omitted",
                "reason": "context_byte_limit",
                "critical": True,
            }],
        },
    }), encoding="utf-8")
    return path


def _report(tmp_path: Path, name: str, *, math_verdict: str = "PASS") -> Path:
    subdir = tmp_path / name
    subdir.mkdir()
    result = aggregate_outputs(
        math_path=_hard(
            subdir / "math.md",
            "math",
            math_verdict,
            1 if math_verdict == "FAIL" else 0,
        ),
        execution_path=_hard(subdir / "execution.md", "execution"),
        paper_path=_paper(subdir / "paper.md"),
    )
    output = subdir / "judge_evaluation.md"
    write_aggregate_report(result, output, base_name=name)
    return output


def test_parse_current_schema_uses_validated_six_dimension_sum(tmp_path):
    parsed = parse_file(_report(tmp_path, "valid"))

    assert parsed["schema_valid"] is True
    assert parsed["comparison_ready"] is True
    assert parsed["total"] == 90
    assert parsed["total_recomputed"] == 90
    assert set(parsed["dims"]) == {
        "model_presentation",
        "solution_narrative",
        "innovation",
        "writing_clarity",
        "result_persuasiveness",
        "sensitivity_limitations",
    }


def test_parse_hard_failure_keeps_only_diagnostic_paper_score(tmp_path):
    parsed = parse_file(_report(tmp_path, "blocked", math_verdict="FAIL"))

    assert parsed["schema_valid"] is True
    assert parsed["comparison_ready"] is False
    assert parsed["total"] is None
    assert parsed["total_recomputed"] is None
    assert parsed["paper_score"] == 90


def test_parse_rejects_first_line_and_machine_verdict_mismatch(tmp_path):
    path = _report(tmp_path, "tampered")
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace("VERDICT: PASS", "VERDICT: REOPEN_REVISION_MODEL", 1), encoding="utf-8")

    parsed = parse_file(path)

    assert parsed["schema_valid"] is False
    assert parsed["comparison_ready"] is False
    assert parsed["total"] is None
    assert parsed["parse_error"] == "first-line verdict does not match aggregate JSON"


def test_legacy_scorecard_is_not_comparison_ready(tmp_path):
    path = tmp_path / "legacy.md"
    path.write_text(
        "VERDICT: PASS\n整体得分: 99/100\n"
        "| 维度 | 权重 | A | B | C | 加权均分 | 评级 |\n"
        "| 模型合理性 | 20% | 20 | 20 | 20 | **20** | 优 |\n",
        encoding="utf-8",
    )
    parsed = parse_file(path)

    assert parsed["status"] == "LEGACY_UNVERIFIED"
    assert parsed["comparison_ready"] is False
    assert parsed["total"] is None
    assert parsed["legacy_total"] == 99


def test_multi_run_aggregate_does_not_drop_a_hard_failure(tmp_path):
    valid = _report(tmp_path, "valid")
    blocked = _report(tmp_path, "blocked", math_verdict="FAIL")
    result = aggregate([valid, blocked])

    assert result["n"] == 2
    assert result["n_scored"] == 1
    assert result["comparison_ready"] is False
    assert result["median_recomputed"] is None
    assert result["diagnostic_median_valid"] == 90


def test_multi_run_aggregate_scores_only_when_every_run_is_ready(tmp_path):
    first = _report(tmp_path, "first")
    second = _report(tmp_path, "second")
    result = aggregate([first, second])

    assert result["comparison_ready"] is True
    assert result["median_total"] == 90
    assert result["median_recomputed"] == 90


def test_parse_preserves_packet_gate_that_forced_indeterminate(tmp_path):
    result = aggregate_outputs(
        math_path=_hard(tmp_path / "math.md", "math"),
        execution_path=_hard(tmp_path / "execution.md", "execution"),
        paper_path=_paper(tmp_path / "paper.md"),
        math_manifest=_incomplete_manifest(tmp_path / "math.manifest.json", "math"),
    )
    report = tmp_path / "judge_evaluation.md"
    write_aggregate_report(result, report, base_name="demo")

    parsed = parse_file(report)

    assert parsed["schema_valid"] is True
    assert parsed["comparison_ready"] is False
    assert parsed["role_statuses"]["math"] == "INDETERMINATE"
