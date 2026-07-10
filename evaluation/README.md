# `evaluation/` — 外部评估闭环（阶段 5.2）

一个**独立于流水线、可复现、可横比**的论文质量评分器。对照
`docs/refactor_plan.md` §5.2。

## 为什么存在

流水线内部的 **Step 13** 与外部评测现在都使用三个隔离评审包：数学审计、执行审计和论文评阅。
数学或执行失败是不可平均的硬否决；论文分数只在正确性通过后用于质量排序。

`evaluation/` 提供同一套 6 维度 rubric、**不同的评判主体**：

- 评委走共享调用器 `scripts/llm_judge_call.py`，默认 **`deepseek-chat`**，按 model 名前缀分派后端（`deepseek*` / `gemini*` / 否则 `claude -p`）。与流水线的 codex / gpt-5.5 谱系不同 → 降低自我偏好。
  （选 DeepSeek 的依据：5.3b 扰动实验显示它对"被破坏的论文"扣分远比 haiku 灵敏——总分扣分率 73% vs 0%；且在本机路由上不像 `opus[1m]` 那样卡死。模型用 `CLAUDE_MODEL` 一行可覆盖，如 `CLAUDE_MODEL=opus`（直连 Anthropic key）或 `=haiku[1m]`。）
- 评委被明确告知它是"外部独立评委"，没看过流水线的内部推理。
- 用重复采样（默认 K=3）取中位数，给出方差，对抗 LLM 评委的随机性。

## 组成

| 文件 | 作用 |
|---|---|
| `run_evaluation.sh` | 主入口：预检门 → 编译 → 隔离评审包 → 三角色 ×K → 否决聚合 |
| `calibration_manifest.json` | 真实获奖论文与生成基线的离线标签、同题奖项顺序和结果路径 |
| `../scripts/evaluate_calibration.py` | 计算奖项顺序准确率、Kendall-style 次序、缺失覆盖、格式失败和致命缺陷检出率 |
| `../scripts/llm_judge_call.py` | 共享 LLM 调用器：按 model 名分派 DeepSeek / Gemini / Claude 三后端（run_evaluation.sh 与 perturbation_harness.py 共用） |
| `../scripts/enrich_evaluation_result.py` | 将聚合结果拆成 `structural` 硬证据和 `llm_score` 软评分 |
| `llm_judge_prompt.txt` | 外部 LLM 评委 prompt（复用 Step 13 rubric + 输出格式） |
| `human_rubric.md` | 给真人评委的纸面打分表（同一把尺子） |
| `baseline_scores.md` | 提交进 git 的校准基线（已完成项目的外部 vs in-loop 读数） |
| `results/` | 每次运行的产物（已 gitignore）：`<base>_eval_run<k>.md` + `<base>_eval.json` |

复用而非重造：
- **rubric** 不重造——6 维度 / 权重是 `STEPS.md:144` 的既定契约，外部与 in-loop 必须同尺。
- 预检用 `scripts/evaluate_modeling_project.py`（结构 / 交付完整性检查器），**先跑它**，FAIL 就不烧评委 token。
- 数值可追溯信号用 `scripts/verify_numbers.py` 的 `UNMATCHED` 计数。
- 评分卡解析用 `scripts/parse_judge_score.py`（in-loop 与外部产物同格式，同一个解析器）。

## 用法

```bash
# 评估一个已完成项目（默认 K=3 次采样）
./evaluation/run_evaluation.sh complete/test_cumcm2024b

# 指定采样次数 / base 名 / 跳过预检门 / 机器可读输出
./evaluation/run_evaluation.sh complete/test_cumcm2024a --samples 5
./evaluation/run_evaluation.sh ongoing/foo my_base --force --json
```

输出（写到 `evaluation/results/`）：
- `<base>_eval_run1.md … run<K>.md` —— 每次评委的完整评分卡。
- `<base>_precheck.json` —— `scripts/evaluate_modeling_project.py --json` 的结构门禁证据。
- `<base>_eval.json` —— 聚合结果，包含：
  - `structural`：`precheck_passed`、`inferred_step`、`unmatched_numbers`、`blocking_evidence`
  - `llm_score`：`median_recomputed`、`min/max/spread_recomputed`、`median_total`、`verdict_distribution`
  - `comparison_ready`：结构门禁通过且至少一个 LLM 样本可解析时为 `true`
- 一行汇总打到 stdout，例如：
  `test_cumcm2024b: external=85.x/100 (spread 84.x-87.x), in-loop=86.4, unmatched=N, precheck=PASS`

## 与 in-loop Step 13 的对照

| | in-loop Step 13 | 外部 evaluation/ |
|---|---|---|
| 评判主体 | 写论文的同一上下文 (codex/gpt 谱系) | 独立调用（默认 `deepseek-chat`，可换 Opus/Sonnet/haiku/gemini） |
| 用途 | 驱动 reopen（控制流） | 客观读数 / 横比 / 消融基线（观察） |
| VERDICT | 驱动 runner 回退 | 仅分类标签，不驱动任何循环 |
| rubric | 6 维度（同） | 6 维度（同） |
| 输出格式 | `judge_evaluation.md` | 同格式（故同一解析器可读） |

## 校准

校准优先看同题真实论文的奖项顺序，不把绝对分数当 ground truth：

```bash
python3 scripts/evaluate_calibration.py evaluation/calibration_manifest.json --existing-results
```

报告会明确列出 `MISSING`，不会把缺失论文从分母静默删除。答案材料不得进入 runtime agent 的评审包。

## 历史结果说明

`results/` 中 2026-07-10 之前的评分卡来自旧的合并上下文评委，只能作为历史样本。新的校准报告会保留这些结果的可用性标记，但新的基线必须由隔离三角色评审重新生成。
