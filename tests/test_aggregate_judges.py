import hashlib
import json
from pathlib import Path

from scripts.aggregate_judges import _artifact_payload, aggregate_outputs, write_aggregate_report


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
            "ref_id": f"{role}-e{index + 1}",
            "claim": f"{role} claim {index + 1}",
            "chunk_id": ("a" if role == "math" else "b") * 64,
            "quote": f"{role} evidence quote {index + 1}",
            "finding": "specific finding",
            "severity": severity,
        }
        for index in range(fatal if verdict == "FAIL" else 1)
    ]
    payload = {
        "schema_version": "judge-hard-role-v2",
        "role": role,
        "verdict": verdict,
        "fatal_flaws": fatal,
        "evidence": evidence,
        "limitations": ["missing independent proof"] if verdict == "INDETERMINATE" else [],
        "conclusion": "audited conclusion",
    }
    return _write(path, f"VERDICT: {verdict}\n{json.dumps(payload)}\n")


def _paper(
    path: Path,
    verdict: str = "PASS",
    scores: dict[str, float] | None = None,
    *,
    issues: list[dict[str, str]] | None = None,
    recommendations: list[str] | None = None,
    schema_version: str = "judge-paper-role-v3",
) -> Path:
    selected = scores or {key: score for key, (_, score) in DIMENSIONS.items()}
    selected_issues = issues
    if selected_issues is None:
        selected_issues = ([{
            "ref_id": "paper-issue-1",
            "severity": "blocking",
            "chunk_id": "f" * 64,
            "quote": "required result is not explained",
            "finding": "required result is not explained",
            "recommendation": "add the missing result narrative",
        }] if verdict == "REVISE" else [])
    payload = {
        "schema_version": schema_version,
        "role": "paper",
        "verdict": verdict,
        "dimensions": {
            key: {
                "score": score,
                "evidence": [{
                    "ref_id": f"paper-{key}",
                    "chunk_id": "c" * 64,
                    "quote": f"paper evidence quote {key}",
                    "finding": "specific evidence",
                }],
            }
            for index, (key, score) in enumerate(selected.items())
        },
        "overall_score": sum(selected.values()),
        "issues": selected_issues,
        "limitations": [],
        "recommendations": ["first"] if recommendations is None else recommendations,
        "conclusion": "paper conclusion",
    }
    if schema_version == "judge-role-v1":
        payload.pop("issues")
    return _write(path, f"VERDICT: {verdict}\n{json.dumps(payload)}\n")


def _manifest(path: Path, role: str, *, complete: bool = True) -> Path:
    context_path = path.with_name("context.txt")
    context_lines = [
        f"{candidate} evidence quote {index}"
        for candidate in ("math", "execution")
        for index in range(1, 4)
    ]
    context_lines.extend(
        f"paper evidence quote {key}" for key in DIMENSIONS
    )
    context_text = "\n".join(context_lines) + "\n"
    context_path.write_text(
        f"\n----- FILE: evidence.txt -----\n{context_text}\n", encoding="utf-8"
    )
    included_sha256 = hashlib.sha256(context_text.encode("utf-8")).hexdigest()
    context_sha256 = hashlib.sha256(context_path.read_bytes()).hexdigest()
    chunk_id = ("a" if role == "math" else "b" if role == "execution" else "c") * 64
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
            **({
                "sha256": included_sha256,
                "included_sha256": included_sha256,
                "included_bytes": len(context_text.encode("utf-8")),
                "chunk_id": chunk_id,
                "source_line_start": 1,
                "source_line_end": len(context_text.splitlines()),
            } if complete else {}),
            **({} if complete else {"reason": "context_byte_limit"}),
        }],
        "context": {
            "sha256": context_sha256,
            "size": context_path.stat().st_size,
        },
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
    assert result.score_available is False
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

    assert result.verdict == "INDETERMINATE_REVIEW"
    assert result.status == "INDETERMINATE"
    assert result.overall_score is None
    assert set(result.indeterminate_roles) == {"math", "execution"}


def test_paper_revision_is_issue_driven_and_score_is_diagnostic(tmp_path):
    result = aggregate_outputs(
        math_path=_hard(tmp_path / "math.md", "math"),
        execution_path=_hard(tmp_path / "execution.md", "execution"),
        paper_path=_paper(tmp_path / "paper.md", "REVISE"),
    )

    assert result.verdict == "REOPEN_REVISION_TEXT"
    assert result.status == "REVISE"
    assert result.overall_score == 90
    assert result.score_available is True
    assert result.score_semantics == "UNCALIBRATED_DIAGNOSTIC"
    assert result.comparison_ready is False


def test_low_diagnostic_score_does_not_force_revision(tmp_path):
    scores = {key: 1 for key in DIMENSIONS}
    result = aggregate_outputs(
        math_path=_hard(tmp_path / "math.md", "math"),
        execution_path=_hard(tmp_path / "execution.md", "execution"),
        paper_path=_paper(tmp_path / "paper.md", "PASS", scores, recommendations=[]),
    )

    assert result.status == "PASS"
    assert result.overall_score == 6
    assert result.score_available is True


def test_paper_recommendation_count_is_not_a_gate(tmp_path):
    result = aggregate_outputs(
        math_path=_hard(tmp_path / "math.md", "math"),
        execution_path=_hard(tmp_path / "execution.md", "execution"),
        paper_path=_paper(tmp_path / "paper.md", recommendations=[]),
    )

    assert result.status == "PASS"


def test_paper_verdict_must_match_blocking_issue_state(tmp_path):
    blocking = [{
        "ref_id": "paper-blocking-1",
        "severity": "blocking",
        "chunk_id": "e" * 64,
        "quote": "required result is absent",
        "finding": "required result is absent",
        "recommendation": "add the required result",
    }]
    result = aggregate_outputs(
        math_path=_hard(tmp_path / "math.md", "math"),
        execution_path=_hard(tmp_path / "execution.md", "execution"),
        paper_path=_paper(tmp_path / "paper.md", "PASS", issues=blocking),
    )

    assert result.status == "INDETERMINATE"
    assert result.verdict == "INDETERMINATE_REVIEW"
    assert result.roles[2].error == "PASS cannot contain a blocking issue"


def test_legacy_paper_schema_is_diagnostic_only(tmp_path):
    result = aggregate_outputs(
        math_path=_hard(tmp_path / "math.md", "math"),
        execution_path=_hard(tmp_path / "execution.md", "execution"),
        paper_path=_paper(tmp_path / "paper.md", schema_version="judge-role-v1"),
    )

    assert result.status == "INDETERMINATE"
    assert result.verdict == "INDETERMINATE_REVIEW"
    assert result.roles[2].status == "LEGACY_UNVERIFIED"
    assert result.paper_score == 90
    assert result.overall_score is None
    assert result.score_available is False


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
    assert '"schema_version": "judge-aggregate-v3"' in text
    assert "整体诊断得分（未校准）: 90/100" in text
    assert "SCORE_AVAILABLE: true" in text
    assert "COMPARISON_READY: false" in text
    assert "SCORE_SEMANTICS: UNCALIBRATED_DIAGNOSTIC" in text


def test_sidecar_artifact_carries_router_and_receipt_provenance(tmp_path):
    result = aggregate_outputs(
        math_path=_hard(tmp_path / "math.md", "math"),
        execution_path=_hard(tmp_path / "execution.md", "execution"),
        paper_path=_paper(tmp_path / "paper.md"),
        math_manifest=_manifest(tmp_path / "math.manifest.json", "math"),
        execution_manifest=_manifest(tmp_path / "execution.manifest.json", "execution"),
        paper_manifest=_manifest(tmp_path / "paper.manifest.json", "paper"),
    )
    payload = _artifact_payload(result)

    assert payload["schema_version"] == "judge-aggregate-v3"
    assert set(payload["packet_completeness"]) == {"math", "execution", "paper"}
    assert {item["role"] for item in payload["roles"]} == {
        "math", "execution", "paper"
    }
    assert payload["role_statuses"] == {
        "math": "PASS", "execution": "PASS", "paper": "PASS"
    }


def test_vetoed_report_displays_na_not_fake_zero_or_paper_score(tmp_path):
    result = aggregate_outputs(
        math_path=_hard(tmp_path / "math.md", "math", "FAIL", 1),
        execution_path=_hard(tmp_path / "execution.md", "execution"),
        paper_path=_paper(tmp_path / "paper.md"),
    )
    output = tmp_path / "judge_evaluation.md"
    write_aggregate_report(result, output, base_name="demo")
    text = output.read_text(encoding="utf-8")

    assert "整体诊断得分（未校准）: N/A" in text
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
    assert result.score_available is True
    assert result.comparison_ready is False
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
