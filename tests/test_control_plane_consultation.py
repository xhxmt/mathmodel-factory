from pathlib import Path

from web.backend.consultation_service import gate_ready, write_consultation_answer


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_gate_ready_matches_runner_heading_format(tmp_path):
    _write(
        tmp_path / "human_review.md",
        "## CONSULT step4 (Step 4) — STATUS: READY\n\n结论。\n",
    )

    assert gate_ready(tmp_path / "human_review.md", "step4") is True


def test_write_consultation_answer_rewrites_runner_compatible_heading(tmp_path):
    _write(tmp_path / "human_review.md", "# 人工审核与介入记录\n")

    write_consultation_answer(
        project_path=tmp_path,
        gate="dynamic",
        step=8,
        title="关键取舍",
        answer="采用方案 B。",
        timestamp="2026-06-27 12:00:00",
    )

    text = (tmp_path / "human_review.md").read_text(encoding="utf-8")
    assert "## CONSULT dynamic (Step 8) — STATUS: READY" in text
    assert "采用方案 B。" in text
