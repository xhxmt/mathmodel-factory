from pathlib import Path


from scripts.aggregate_judges import aggregate_outputs, write_aggregate_report


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_math_failure_vetoes_high_paper_score(tmp_path):
    result = aggregate_outputs(
        math_path=_write(tmp_path / "math.md", "VERDICT: FAIL\nFATAL_FLAWS: 1\n"),
        execution_path=_write(
            tmp_path / "execution.md", "VERDICT: PASS\nFATAL_FLAWS: 0\n"
        ),
        paper_path=_write(tmp_path / "paper.md", "VERDICT: PASS\nSCORE: 96\n"),
    )

    assert result.verdict == "REOPEN_REVISION_MODEL"
    assert result.status == "FAIL"
    assert result.paper_score == 96
    assert "math" in result.vetoes


def test_execution_failure_vetoes_paper_pass(tmp_path):
    result = aggregate_outputs(
        math_path=_write(tmp_path / "math.md", "VERDICT: PASS\nFATAL_FLAWS: 0\n"),
        execution_path=_write(
            tmp_path / "execution.md", "VERDICT: FAIL\nFATAL_FLAWS: 2\n"
        ),
        paper_path=_write(tmp_path / "paper.md", "VERDICT: PASS\nSCORE: 88\n"),
    )

    assert result.verdict == "REOPEN_REVISION_MODEL"
    assert result.status == "FAIL"
    assert "execution" in result.vetoes


def test_missing_or_malformed_output_is_indeterminate_not_pass(tmp_path):
    result = aggregate_outputs(
        math_path=tmp_path / "missing.md",
        execution_path=_write(tmp_path / "execution.md", "not a verdict"),
        paper_path=_write(tmp_path / "paper.md", "VERDICT: PASS\nSCORE: 90\n"),
    )

    assert result.verdict == "REOPEN_REVISION_MODEL"
    assert result.status == "INDETERMINATE"
    assert set(result.indeterminate_roles) == {"math", "execution"}


def test_paper_revision_only_reopens_text_when_correctness_passes(tmp_path):
    result = aggregate_outputs(
        math_path=_write(tmp_path / "math.md", "VERDICT: PASS\nFATAL_FLAWS: 0\n"),
        execution_path=_write(
            tmp_path / "execution.md", "VERDICT: PASS\nFATAL_FLAWS: 0\n"
        ),
        paper_path=_write(tmp_path / "paper.md", "VERDICT: REVISE\nSCORE: 68\n"),
    )

    assert result.verdict == "REOPEN_REVISION_TEXT"
    assert result.status == "REVISE"


def test_aggregate_report_preserves_gate2_first_line(tmp_path):
    result = aggregate_outputs(
        math_path=_write(tmp_path / "math.md", "VERDICT: PASS\nFATAL_FLAWS: 0\n"),
        execution_path=_write(
            tmp_path / "execution.md", "VERDICT: PASS\nFATAL_FLAWS: 0\n"
        ),
        paper_path=_write(tmp_path / "paper.md", "VERDICT: PASS\nSCORE: 82.5\n"),
    )
    output = tmp_path / "judge_evaluation.md"
    write_aggregate_report(result, output, base_name="demo")

    text = output.read_text(encoding="utf-8")
    assert text.splitlines()[0] == "VERDICT: PASS"
    assert "整体得分: 82.5/100" in text
    assert "Correctness vetoes: none" in text
