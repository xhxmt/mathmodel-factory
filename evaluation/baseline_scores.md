# 外部评估基线状态 — Baseline Scores

> **当前状态（2026-07-17）：没有可用于绝对分或奖级预测的活跃基线。**
>
> 本页保留的 2026-06 读数来自旧合并上下文评分卡，统一视为 `LEGACY_UNVERIFIED`。
> 它们缺少当前 `judge-role-v1` / `judge-aggregate-v1`、数学与执行硬角色状态、packet / prompt / 配置指纹及最终提交输入指纹，因此不能与当前结果横比，也不能继续作为消融效应或模型精度的 ground truth。

## 当前基线准入条件

新的基线条目必须同时满足：

1. 对 Step 15 后的最终提交输入评分，且 `judge_outputs/final_submission.sha256` 与当前三角色 packet、论文资产和实际交付 PDF 字节一致；
2. 数学审计和执行审计均为 `PASS`，没有 `FAIL` 或 `INDETERMINATE`；
3. 论文角色与聚合结果分别满足当前严格 schema，所有请求运行都可解析；
4. 结果位于不可变 `evaluation/results/runs/<base>/<run_id>/`，并记录模型、prompt、实现和 packet 的配置指纹；
5. 校准报告身份新鲜且与运行模型 / schema 匹配；
6. 只在 `comparison_ready_proxy=true` 时做已知退化范围内的 A/B 诊断；只在 `comparison_ready_human=true` 时做有人类真值支持的质量横比；只有 `award_prediction_ready=true` 时才讨论奖级。

在这些条件补齐前，`/100` 只能是条件论文质量的诊断读数，不能解释为获奖概率或真实评委绝对分。

## K 次调用的正确解释

旧记录使用 K=3 并报告 min/max spread。当前 API 评委路径使用 `temperature=0`，所以同一模型、同一 prompt、同一 packet 的 K 次调用不是独立统计重复：

- 低 spread 不证明评分器可靠；
- spread 不等于置信区间；
- 三次相同结果不等于生成方差或人类评委方差为零；
- K 次调用的当前用途是暴露格式失败、角色不一致和后端非确定性；任何一轮硬 FAIL / INDETERMINATE 都不能被其余轮次的中位数丢弃。

真正的可靠性必须来自外部人类真值、位置平衡的盲比、致命错误的 sensitivity / specificity / precision，以及跨题型留出集。

## 历史读数（`LEGACY_UNVERIFIED`）

下列数据只用于重建旧实验发生过什么，不代表当前评分轴。

| 项目 | 旧外部中位数 | 旧 spread | 旧评委手写总分 | 旧 in-loop 读数 | 当前状态 |
|---|---:|---:|---:|---:|---|
| `test_cumcm2024a` | 66.3 | 65.3–69.0 | 68.2 | 80.2 | `LEGACY_UNVERIFIED` |
| `test_cumcm2024b` | 70.6 | 70.3–74.0 | 68.2 | 86.4 | `LEGACY_UNVERIFIED` |

旧六维读数如下，同样不可与当前“模型呈现 / 求解叙事 / 敏感性与局限”schema 混用：

| 旧维度 | 2024a 外部 | 2024a in-loop | 2024b 外部 | 2024b in-loop |
|---|---:|---:|---:|---:|
| 模型合理性 (20) | 13.0 | 16.7 | 13.0 | 17.7 |
| 求解正确性 (20) | 11.0 | 16.3 | 14.0 | 17.7 |
| 创新性 (20) | 15.0 | 16.7 | 15.0 | 17.3 |
| 写作清晰度 (15) | 11.0 | 10.7 | 11.0 | 12.7 |
| 结果说服力 (15) | 9.0 | 12.7 | 11.0 | 13.7 |
| 灵敏度分析 (10) | 6.3 | 8.7 | 6.3 | 9.0 |

历史上 `2024b > 2024a` 的顺序一致和区间不重叠，只能称为这六次旧调用中的观察现象。由于样本不是独立统计重复、评分契约已经变化且没有足量人类真值，不能据此宣称“区分度稳定”或“统计显著”。

## 旧评委行为记录

- `deepseek-chat` 的手写总分曾集中在 68.2–68.4，而旧六维重算和为 65.3–74.0，显示明显总分锚定。
- 旧输出曾出现维度超出满分；旧解析器用夹紧和重算维持数值可用性。当前严格 schema 不再静默夹紧，越界或总分不等于六维和会使角色结果 INDETERMINATE。
- 旧扰动实验中，haiku 的总分扣分检出率为 0/15，DeepSeek 为 11/15；这只支持“DeepSeek 在该旧代理集上更敏感”，不等价于人类评分准确率或奖级校准。

## 查看旧结果

```bash
# 当前解析器会保留旧数字用于诊断，同时返回 legacy=true、
# status=LEGACY_UNVERIFIED、comparison_ready=false。
python3 scripts/parse_judge_score.py complete/test_cumcm2024a/judge_evaluation.md
python3 scripts/parse_judge_score.py complete/test_cumcm2024b/judge_evaluation.md
```

不要把上述命令的 legacy 数字复制到新的对比报告。新基线应通过当前 `evaluation/run_evaluation.sh` 生成不可变运行，并在本页另建“当前 schema 基线”章节；在此之前保持空缺比伪造可比性更可靠。

## haiku 历史附录（已归档）

| 项目 | 旧外部中位数 | 旧 spread | n_scored | 当前状态 |
|---|---:|---:|---:|---|
| `test_cumcm2024a` | 73.0 | 65.0–81.0 | 2/3 | `LEGACY_UNVERIFIED` |
| `test_cumcm2024b` | 94.7 | 86.0–99.3 | 3/3 | `LEGACY_UNVERIFIED` |

这些读数还包含首行格式失败、维度溢出和路由超时等已知问题，只保留为历史诊断材料。
