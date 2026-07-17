import json
from pathlib import Path

from scripts.aggregate_judges import aggregate_outputs, write_aggregate_report


DIMENSIONS = {
    "model_presentation": (20, 18),
    "solution_narrative": (20, 18),
    "innovation": (20, 17),
    "writing_clarity": (15, 14),
    "result_persuasiveness": (15, 14),
    "sensitivity_limitations": (10, 9),
}


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def _hard(path: Path, role: str, verdict: str = "PASS", fatal: int = 0) -> Path:
    severity = "fatal" if verdict == "FAIL" else "support"
    evidence = [
        {
            "claim": f"{role} claim {index + 1}",
            "location": f"context.txt:{index + 1}",
            "finding": "specific finding",
            "severity": severity,
        }
        for index in range(fatal if verdict == "FAIL" else 1)
    ]
    payload = {
        "schema_version": "judge-role-v1",
        "role": role,
        "verdict": verdict,
        "fatal_flaws": fatal,
        "evidence": evidence,
        "limitations": ["missing independent proof"] if verdict == "INDETERMINATE" else [],
        "conclusion": "audited conclusion",
    }
    return _write(path, f"VERDICT: {verdict}\n{json.dumps(payload)}\n")


def _paper(path: Path, verdict: str = "PASS", scores: dict[str, float] | None = None) -> Path:
    selected = scores or {key: score for key, (_, score) in DIMENSIONS.items()}
    payload = {
        "schema_version": "judge-role-v1",
        "role": "paper",
        "verdict": verdict,
        "dimensions": {
            key: {
                "score": score,
                "evidence": [{"location": f"paper:{key}", "finding": "specific evidence"}],
            }
            for key, score in selected.items()
        },
        "overall_score": sum(selected.values()),
        "limitations": [],
        "recommendations": ["first", "second", "third"],
        "conclusion": "paper conclusion",
    }
    return _write(path, f"VERDICT: {verdict}\n{json.dumps(payload)}\n")


def _manifest(path: Path, role: str, *, complete: bool = True) -> Path:
    requirement = {
        "id": "primary_evidence",
        "description": "test evidence",
        "required_status": "included",
        "paths": ["evidence.txt"],
        "satisfied_paths": ["evidence.txt"] if complete else [],
        "satisfied": complete,
    }
    if not complete:
        requirement["failure_reason"] = "required_artifact_not_fully_included"
    payload = {
        "role": role,
        "files": [{
            "path": "evidence.txt",
            "status": "included" if complete else "omitted",
            **({} if complete else {"reason": "context_byte_limit"}),
        }],
        "completeness": {
            "contract_version": "judge-packet-completeness-v1",
            "status": "COMPLETE" if complete else "INCOMPLETE",
            "eligible": complete,
            "requirements": [requirement],
            "limitations": [] if complete else [{
                "path": "evidence.txt",
                "status": "omitted",
                "reason": "context_byte_limit",
                "critical": True,
            }],
        },
    }
    return _write(path, json.dumps(payload))


def test_math_failure_vetoes_and_removes_comparable_score(tmp_path):
    result = aggregate_outputs(
        math_path=_hard(tmp_path / "math.md", "math", "FAIL", 1),
        execution_path=_hard(tmp_path / "execution.md", "execution"),
        paper_path=_paper(tmp_path / "paper.md"),
    )

    assert result.verdict == "REOPEN_REVISION_MODEL"
    assert result.status == "FAIL"
    assert result.paper_score == 90
    assert result.overall_score is None
    assert result.comparison_ready is False
    assert "math" in result.vetoes


def test_execution_failure_vetoes_paper_pass(tmp_path):
    result = aggregate_outputs(
        math_path=_hard(tmp_path / "math.md", "math"),
        execution_path=_hard(tmp_path / "execution.md", "execution", "FAIL", 2),
        paper_path=_paper(tmp_path / "paper.md"),
    )

    assert result.verdict == "REOPEN_REVISION_MODEL"
    assert result.status == "FAIL"
    assert result.overall_score is None
    assert "execution" in result.vetoes


def test_missing_or_malformed_output_is_indeterminate_not_pass(tmp_path):
    result = aggregate_outputs(
        math_path=tmp_path / "missing.md",
        execution_path=_write(tmp_path / "execution.md", "VERDICT: PASS\nnot-json\n"),
        paper_path=_paper(tmp_path / "paper.md"),
    )

    assert result.verdict == "REOPEN_REVISION_MODEL"
    assert result.status == "INDETERMINATE"
    assert result.overall_score is None
    assert set(result.indeterminate_roles) == {"math", "execution"}


def test_paper_revision_is_comparable_only_after_correctness_passes(tmp_path):
    scores = {key: score - 4 for key, (_, score) in DIMENSIONS.items()}
    result = aggregate_outputs(
        math_path=_hard(tmp_path / "math.md", "math"),
        execution_path=_hard(tmp_path / "execution.md", "execution"),
        paper_path=_paper(tmp_path / "paper.md", "REVISE", scores),
    )

    assert result.verdict == "REOPEN_REVISION_TEXT"
    assert result.status == "REVISE"
    assert result.overall_score == 66
    assert result.comparison_ready is True


def test_strict_paper_schema_rejects_score_sum_mismatch(tmp_path):
    paper = _paper(tmp_path / "paper.md")
    lines = paper.read_text(encoding="utf-8").splitlines()
    payload = json.loads(lines[1])
    payload["overall_score"] = 99
    paper.write_text(f"VERDICT: PASS\n{json.dumps(payload)}\n", encoding="utf-8")

    result = aggregate_outputs(
        math_path=_hard(tmp_path / "math.md", "math"),
        execution_path=_hard(tmp_path / "execution.md", "execution"),
        paper_path=paper,
    )

    assert result.status == "INDETERMINATE"
    assert result.roles[2].error == "overall_score must equal the sum of six dimension scores"
    assert result.overall_score is None


def test_strict_role_envelope_rejects_trailing_prose(tmp_path):
    math_path = _hard(tmp_path / "math.md", "math")
    math_path.write_text(math_path.read_text(encoding="utf-8") + "extra prose\n", encoding="utf-8")
    result = aggregate_outputs(
        math_path=math_path,
        execution_path=_hard(tmp_path / "execution.md", "execution"),
        paper_path=_paper(tmp_path / "paper.md"),
    )

    assert result.roles[0].status == "INDETERMINATE"
    assert "invalid JSON payload" in (result.roles[0].error or "")


def test_aggregate_report_preserves_first_line_and_machine_schema(tmp_path):
    result = aggregate_outputs(
        math_path=_hard(tmp_path / "math.md", "math"),
        execution_path=_hard(tmp_path / "execution.md", "execution"),
        paper_path=_paper(tmp_path / "paper.md"),
    )
    output = tmp_path / "judge_evaluation.md"
    write_aggregate_report(result, output, base_name="demo")

    text = output.read_text(encoding="utf-8")
    assert text.splitlines()[0] == "VERDICT: PASS"
    assert "<!-- JUDGE_AGGREGATE_JSON_BEGIN -->" in text
    assert "整体得分: 90/100" in text
    assert "COMPARISON_READY: true" in text


def test_vetoed_report_displays_na_not_fake_zero_or_paper_score(tmp_path):
    result = aggregate_outputs(
        math_path=_hard(tmp_path / "math.md", "math", "FAIL", 1),
        execution_path=_hard(tmp_path / "execution.md", "execution"),
        paper_path=_paper(tmp_path / "paper.md"),
    )
    output = tmp_path / "judge_evaluation.md"
    write_aggregate_report(result, output, base_name="demo")
    text = output.read_text(encoding="utf-8")

    assert "整体得分: N/A" in text
    assert "Paper diagnostic score: 90.0" in text
    assert "COMPARISON_READY: false" in text


def test_incomplete_packet_overrides_model_pass_to_indeterminate(tmp_path):
    result = aggregate_outputs(
        math_path=_hard(tmp_path / "math.md", "math"),
        execution_path=_hard(tmp_path / "execution.md", "execution"),
        paper_path=_paper(tmp_path / "paper.md"),
        math_manifest=_manifest(tmp_path / "math.manifest.json", "math", complete=False),
        execution_manifest=_manifest(tmp_path / "execution.manifest.json", "execution"),
        paper_manifest=_manifest(tmp_path / "paper.manifest.json", "paper"),
    )

    assert result.status == "INDETERMINATE"
    assert result.comparison_ready is False
    assert result.roles[0].status == "INDETERMINATE"
    assert result.roles[0].verdict == "INDETERMINATE"
    assert "primary_evidence" in (result.roles[0].error or "")
    assert result.packet_completeness["math"]["eligible"] is False


def test_complete_packet_manifests_preserve_valid_role_results(tmp_path):
    result = aggregate_outputs(
        math_path=_hard(tmp_path / "math.md", "math"),
        execution_path=_hard(tmp_path / "execution.md", "execution"),
        paper_path=_paper(tmp_path / "paper.md"),
        math_manifest=_manifest(tmp_path / "math.manifest.json", "math"),
        execution_manifest=_manifest(tmp_path / "execution.manifest.json", "execution"),
        paper_manifest=_manifest(tmp_path / "paper.manifest.json", "paper"),
    )

    assert result.status == "PASS"
    assert result.comparison_ready is True
    assert all(item["eligible"] is True for item in result.packet_completeness.values())


def test_manifest_cannot_claim_complete_when_required_file_is_not_included(tmp_path):
    manifest = _manifest(tmp_path / "math.manifest.json", "math")
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["files"][0]["status"] = "omitted"
    payload["files"][0]["reason"] = "context_byte_limit"
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    result = aggregate_outputs(
        math_path=_hard(tmp_path / "math.md", "math"),
        execution_path=_hard(tmp_path / "execution.md", "execution"),
        paper_path=_paper(tmp_path / "paper.md"),
        math_manifest=manifest,
    )

    assert result.roles[0].status == "INDETERMINATE"
    assert "paths conflict" in (result.roles[0].error or "")
