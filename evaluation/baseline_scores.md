# 外部评估校准基线 — Baseline Scores

> 由 `evaluation/run_evaluation.sh` 在两道已完成真题上跑出的**第一份客观外部读数**，
> 提交进 git 作为后续 prompt 迭代（5.3）/ 消融实验（5.4）的对照基线。
> 这不是流水线自评（in-loop Step 13），而是**独立外部评委**给出的分数。

## 运行配置（可复现）

| 项 | 值 |
|---|---|
| 日期 | 2026-06-02 |
| 评委模型 | `haiku[1m]`（见下方"模型决策"——`opus[1m]` 在本机路由上卡死，不可用） |
| effort | `high`（继承自环境 `CLAUDE_EFFORT`） |
| 采样 | K=3，取中位数；记录 min/max spread |
| 入参 | 论文 `.tex` + 6 个证据文件 + `verify_numbers` 的 UNMATCHED 计数 |
| 命令 | `JUDGE_TIMEOUT=360 ./evaluation/run_evaluation.sh complete/<base> --samples 3` |

## 结果

| 项目 | 外部中位数 | 外部 spread (min–max) | n_scored | in-loop ground truth | 偏差(外-内) |
|---|---:|---:|---:|---:|---:|
| `test_cumcm2024a` | **73.0** | 65.0 – 81.0 | 2/3 | 80.2（良） | −7.2 |
| `test_cumcm2024b` | **94.7** | 86.0 – 99.3 | 3/3 | 86.4（优） | +8.3 |

### 健全性约束：序保持 ✅

外部 `2024b (94.7) > 2024a (73.0)`，与 in-loop 序（`86.4 > 80.2`）一致 → **未翻转，不报警**。

更强的信号：两篇论文的**得分区间完全不重叠**（2024a 65–81 vs 2024b 86–99.3）——
即每一个 2024b 采样都高于每一个 2024a 采样。尽管单次方差大，**区分度是稳的**。

### 各维度中位数（满分：模型/求解/创新=20，写作/说服力=15，灵敏度=10）

| 维度 | 2024a 外部 | 2024a in-loop | 2024b 外部 | 2024b in-loop |
|---|---:|---:|---:|---:|
| 模型合理性 (20) | 17.0 | 16.7 | 17.0 | 17.7 |
| 求解正确性 (20) | 15.5 | 16.3 | 17.3 | 17.7 |
| 创新性 (20) | 15.5 | 16.7 | 16.7 | 17.3 |
| 写作清晰度 (15) | 12.85 | 10.7 | 14.67 | 12.7 |
| 结果说服力 (15) | 12.35 | 12.7 | 15.67 | 13.7 |
| 灵敏度分析 (10) | 8.35 | 8.7 | 9.7 | 9.0 |

## 诚实的局限（必须随基线一起记）

1. **评委方差大。** `haiku[1m]` 单次打分波动达 13–16 分（2024a 65↔81，2024b 86↔99.3）。
   K=3 中位数是既定的方差对策，但 haiku 的噪声明显高于 plan 设想的 Opus。
   **生产/正式横比建议 K≥5，或在更强评委可用时改用之。**
2. **偶发格式违规。** 本轮观察到两类 haiku 输出瑕疵：
   - 2024a 有 1/3 次的首行不是 `VERDICT:`（被解析器判为无分，未计入中位数 → n_scored=2）。
   - 2024b 有 1 次把"灵敏度分析"打成 **18.33/10**（超过该维度满分 10），把该次总分抬到 99.3。
   这两类都说明 haiku 对输出契约的遵守不如大模型稳。解析器对前者是安全的（丢弃无分项），
   后者目前**不会**被自动夹紧——属已知风险，留待 5.3 prompt 加固或换评委时处理。
3. **外部 vs in-loop 关系非单调。** 2024a 外部更低（−7.2，符合"独立评委更严"的预期），
   但 2024b 外部更高（+8.3，受上面 18.33 异常与 haiku 顶端给分偏松影响）。
   所以**外部分数的绝对值不能直接替代 in-loop 数值**；它的价值在于**序 / 区分度 / 独立视角**，
   而非逐分对齐。

## 模型决策：为什么是 `haiku[1m]` 而不是 plan 里写的 Opus

本机通过第三方路由 `anyrouter.top`（`ANTHROPIC_BASE_URL`）访问模型，实测：

| 模型 | 轻量请求 | 重型评委请求 |
|---|---|---|
| `opus[1m]`（仓库默认） | 6s 正常返回 | **卡死**：300s 0 字节、从不吐 token（reasoning 阶段就停住） |
| `sonnet`（无 [1m]） | 即时 400「请启用 1m 上下文」 | 同样 400 |
| `sonnet[1m]` | 连 PONG 都超时 | — |
| `haiku[1m]` | 6s 正常 | **~40–150s 出完整评分卡** ✅ |

结论：在本路由上 `opus[1m]` 对"读论文+多维度推理打分"这类重型生成不可用，
而 `haiku[1m]` 是唯一能稳定完成的模型。`haiku` 与流水线的 `gpt-5.5/codex` 谱系不同，
self-preference 偏置的初衷仍成立。模型是 `CLAUDE_MODEL` 一行可覆盖的旋钮：
若指向直连 Anthropic key 的端点，可 `CLAUDE_MODEL=opus`（或 `sonnet`）切回更强评委，
届时方差应显著收窄、绝对值更接近 in-loop。

## 复现

```bash
# 解析器对 in-loop 文件应得 80.2 / 86.4
python3 scripts/parse_judge_score.py complete/test_cumcm2024a/judge_evaluation.md
python3 scripts/parse_judge_score.py complete/test_cumcm2024b/judge_evaluation.md

# 重跑外部校准（每跑因 LLM 随机性会有方差；序应保持 2024b > 2024a）
JUDGE_TIMEOUT=360 ./evaluation/run_evaluation.sh complete/test_cumcm2024a --samples 3
JUDGE_TIMEOUT=360 ./evaluation/run_evaluation.sh complete/test_cumcm2024b --samples 3
```
