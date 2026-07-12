from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_step8_prompt_matches_current_figure_style_contract() -> None:
    prompt = (ROOT / "prompts" / "step8_visualization.txt").read_text(encoding="utf-8")
    guide = (ROOT / "modeling_guide.md").read_text(encoding="utf-8")

    assert "Times New Roman 或与论文正文匹配的衬线字体" in prompt
    assert "浅灰虚线主网格" in prompt
    assert "PNG @ 600dpi" in prompt
    assert "Major gridlines only, dashed light gray." in guide
    assert "Times New Roman or another serif" in guide
    assert "网格关闭" not in prompt
    assert "不允许 Times" not in prompt
