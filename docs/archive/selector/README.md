# Selector 放权计划（已归档，2026-11-14）

这里是 R0a → R1/R2-min → R0b → R3 → 人工放权这条 selector 路线的计划和合同文档。
对应脚本（`capability_harness.py`、`hard_gate_calibration.py`、`selector_calibration.py`、
`shadow_portfolio.py`、`selector_cutover_authorization.py`、`shadow_cutover.py`）于
2026-08-08 实现并通过本地测试，但始终没有在真实数据上运行，也没有产出任何报告；
运行路径和 `evaluation/run_evaluation.sh` 都不读取它们。2026-11-14 删除这些脚本，
文档移到这里保留。

要恢复这条路线，从提交 `b80570a` 取回脚本和测试，并按 `SELECTOR_ROLLOUT_PLAN.md`
的状态矩阵重新核对依赖（尤其是评审身份和 packet 合同在 8 月之后已经变化）。

仍在使用的部分没有动：`scripts/judge_reliability.py`（`run_evaluation.sh` 调用）、
`scripts/calibration_judge.py` / `evaluate_calibration.py` / `proxy_calibration.py`
（生成 `run_evaluation.sh` 读取的 `evaluation/proxy_calibration_report.json`）。
