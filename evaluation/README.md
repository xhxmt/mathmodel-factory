# `evaluation/` — 外部评估闭环（阶段 5.2）

一个**独立于流水线、可复现、可横比**的论文质量评分器。对照
`docs/refactor_plan.md` §5.2。

## 为什么存在

流水线内部的 **Step 13 评委模拟**（`prompts/step13_gate2_judge.txt`）由"写论文的同一上下文"
打分，用于驱动 reopen——但做横向比较 / 消融实验时，这种自评有 self-preference 偏置，**不可信**。

`evaluation/` 提供同一套 6 维度 rubric、**不同的评判主体**：

- 评委走共享调用器 `scripts/llm_judge_call.py`，默认 **`deepseek-chat`**，按 model 名前缀分派后端（`deepseek*` / `gemini*` / 否则 `claude -p`）。与流水线的 codex / gpt-5.5 谱系不同 → 降低自我偏好。
  （选 DeepSeek 的依据：5.3b 扰动实验显示它对"被破坏的论文"扣分远比 haiku 灵敏——总分扣分率 73% vs 0%；且在本机路由上不像 `opus[1m]` 那样卡死。模型用 `CLAUDE_MODEL` 一行可覆盖，如 `CLAUDE_MODEL=opus`（直连 Anthropic key）或 `=haiku[1m]`。）
- 评委被明确告知它是"外部独立评委"，没看过流水线的内部推理。
- 用重复采样（默认 K=3）取中位数，给出方差，对抗 LLM 评委的随机性。

## 组成

| 文件 | 作用 |
|---|---|
| `run_evaluation.sh` | 主入口：预检门 → 编译 → 数值核查 → 评委 ×K → 聚合 |
| `../scripts/llm_judge_call.py` | 共享 LLM 调用器：按 model 名分派 DeepSeek / Gemini / Claude 三后端（run_evaluation.sh 与 perturbation_harness.py 共用） |
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
- `<base>_eval.json` —— 聚合：median / min / max 总分、各次 VERDICT、verify_numbers 的 unmatched 计数、预检结果。
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

`baseline_scores.md` 记录已完成项目的外部分数与 in-loop ground truth 的偏差。
健全性约束：外部分数应保持 **2024b > 2024a** 的序（in-loop: 86.4 > 80.2）。
若翻转 → 评委 prompt 失准，需回炉。

## 不在本阶段范围

- 对照获奖论文打分（`benchmark/` 里没有，且版权不可 commit）→ 留作扩展。
- prompt 迭代记录（5.3 `prompts/CHANGELOG.md`）、消融脚本（5.4 `experiments/`）、研究论文素材（5.5）。
  但本目录的 `<base>_eval.json` 是 5.4 消融统计的共用前置。
