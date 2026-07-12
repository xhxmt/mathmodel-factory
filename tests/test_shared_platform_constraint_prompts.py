from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_step5_and_step10_enforce_shared_platform_decisions() -> None:
    step5 = (ROOT / "prompts" / "step5_full_solve.txt").read_text(encoding="utf-8")
    step10 = (ROOT / "prompts" / "step10_gate1_numerical.txt").read_text(encoding="utf-8")

    assert "同一 `uav_id` 的所有烟幕记录必须共享唯一航向和唯一速度" in step5
    assert "不得直接拼接成 canonical 策略" in step5
    assert "按 `uav_id` 分组" in step10
    assert "禁止仅修改表格显示值" in step10
