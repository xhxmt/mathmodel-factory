from pathlib import Path

from factory_core.storage import SQLiteStateStore
from web.backend.consultation_service import gate_ready, write_consultation_answer


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_gate_ready_rejects_mutable_heading_without_sqlite_projection(tmp_path):
    _write(
        tmp_path / "human_review.md",
        "## CONSULT step4 (Step 4) — STATUS: READY\n\n结论。\n",
    )

    assert gate_ready(tmp_path / "human_review.md", "step4") is False


def test_gate_ready_accepts_only_deterministic_projection_marker(tmp_path):
    _write(
        tmp_path / "human_review.md",
        "<!-- FACTORY_CONSULTATION_step4_START -->\n"
        "## CONSULT step4 — STATUS: READY\n"
        "SOURCE_OF_TRUTH: .factory/state.db\n"
        "<!-- FACTORY_CONSULTATION_step4_END -->\n",
    )

    assert gate_ready(tmp_path / "human_review.md", "step4") is True


def test_write_consultation_answer_stages_without_publishing_ready_answer(tmp_path):
    SQLiteStateStore(tmp_path).initialize(
        project_id="demo", project_type="modeling"
    )
    _write(tmp_path / "human_review.md", "# 人工审核与介入记录\n")

    staged = write_consultation_answer(
        project_path=tmp_path,
        gate="dynamic",
        step=8,
        title="关键取舍",
        answer="采用方案 B。",
        timestamp="2026-06-27 12:00:00",
        request_id="request-1",
    )

    text = (tmp_path / "human_review.md").read_text(encoding="utf-8")
    assert staged.is_file()
    assert "STATUS: PENDING_SQLITE_CAS" in text
    assert "STATUS: READY" not in text
    assert "采用方案 B。" not in text
    assert gate_ready(tmp_path / "human_review.md", "dynamic") is False


def test_legacy_write_consultation_answer_preserves_ready_projection(tmp_path):
    review = write_consultation_answer(
        project_path=tmp_path,
        gate="preflight",
        step=0,
        title="Preflight",
        answer="Proceed",
        timestamp="2026-06-27 12:00:00",
    )

    text = review.read_text(encoding="utf-8")
    assert "## CONSULT preflight (Step 0) — STATUS: READY" in text
    assert "Proceed" in text
    assert not (tmp_path / ".factory" / "state.db").exists()
    assert not (tmp_path / ".factory" / "decision_staging").exists()
