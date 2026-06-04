# 外部评估校准基线 — Baseline Scores

> 由 `evaluation/run_evaluation.sh` 在两道已完成真题上跑出的客观外部读数，
> 提交进 git 作为后续 prompt 迭代（5.3）/ 消融实验（5.4）的对照基线。
> 这不是流水线自评（in-loop Step 13），而是**独立外部评委**给出的分数。
>
> **当前基线评委：`deepseek-chat`**（2026-06-04 重跑）。早期 `haiku[1m]` 读数见文末
> "附录 A：haiku 时代基线（已归档）"，保留作历史对照，不再作为活跃基线。

## 运行配置（可复现）

| 项 | 值 |
|---|---|
| 日期 | 2026-06-04 |
| 评委模型 | `deepseek-chat`（DeepSeek API，`DEEPSEEK_API_KEY`；经 `scripts/llm_judge_call.py` 分派） |
| 采样 | K=3，取中位数；记录 min/max spread |
| 入参 | 论文 `.tex` + 6 个证据文件 + `verify_numbers` 的 UNMATCHED 计数 |
| 命令 | `JUDGE_TIMEOUT=300 ./evaluation/run_evaluation.sh complete/<base> --samples 3` |
| 总分口径 | **`median_recomputed`**（夹紧后六维加权均分之和），而非评委手写的 `整体得分`——理由见下方"评委的总分锚定"。 |

## 结果

| 项目 | 外部中位数(重算) | 外部 spread | 评委手写总分(锚定) | in-loop ground truth |
|---|---:|---:|---:|---:|
| `test_cumcm2024a` | **66.3** | 65.3 – 69.0 | 68.2 | 80.2（良） |
| `test_cumcm2024b` | **70.6** | 70.3 – 74.0 | 68.2 | 86.4（优） |

### 健全性约束：序保持 ✅

外部 `2024b (70.6) > 2024a (66.3)`，与 in-loop 序（`86.4 > 80.2`）一致 → **未翻转**。
两篇论文的**得分区间完全不重叠**（2024a 65.3–69.0 vs 2024b 70.3–74.0）——
即每一个 2024b 采样都高于每一个 2024a 采样。DeepSeek 的方差极小（spread < 4 分，
remarkably 比 haiku 的 13–16 分窄），区分度稳。

### 各维度中位数（满分：模型/求解/创新=20，写作/说服力=15，灵敏度=10）

| 维度 | 2024a 外部 | 2024a in-loop | 2024b 外部 | 2024b in-loop |
|---|---:|---:|---:|---:|
| 模型合理性 (20) | 13.0 | 16.7 | 13.0 | 17.7 |
| 求解正确性 (20) | 11.0 | 16.3 | 14.0 | 17.7 |
| 创新性 (20) | 15.0 | 16.7 | 15.0 | 17.3 |
| 写作清晰度 (15) | 11.0 | 10.7 | 11.0 | 12.7 |
| 结果说服力 (15) | 9.0 | 12.7 | 11.0 | 13.7 |
| 灵敏度分析 (10) | 6.3 | 8.7 | 6.3 | 9.0 |

维度层面 2024b 在求解(14 vs 11)、说服力(11 vs 9)上明显占优，这正是序得以保持的来源。
DeepSeek 普遍比 in-loop 严 3–5 分/维度，符合"独立外部评委更苛刻"的预期。

## 诚实的局限（必须随基线一起记）

1. **评委的总分锚定（DeepSeek 的核心怪癖）。** `deepseek-chat` 倾向于把手写的
   `整体得分:` 锁在一个近乎常数的值上，几乎不随它自己的六维评分变化——本轮 6 个 run 的
   手写总分全在 **68.2–68.4**（方差 0.2），而底层夹紧六维加权和却从 65.3 跨到 74.0（跨度 8.7）。
   若直接采信手写总分，两篇会假性打平、序崩。**对策：基线总分一律用 `median_recomputed`**
   （夹紧后六维加权均分之和，六维满分合计 100 → 本身就是 0–100 分）。该口径在两个 in-loop
   文件上验证为 81.8 / 88.1，与评委手写的 80.2 / 86.4 仅差恒定 ~1.6（手算舍入），序一致。
   解析器（`scripts/parse_judge_score.py`）现同时输出 `total`（手写，向后兼容）、
   `total_adjusted`（手写减溢出）、`total_recomputed`（横比用）。
2. **维度满分溢出。** DeepSeek 偶发把"灵敏度分析"打成 13/10 或 14/10（6 个 run 中 2 次）。
   解析器现**自动夹紧**到维度满分并在 `overflow_clamped` / `any_clamped` 标记，溢出量从
   `total_adjusted` / `total_recomputed` 中剔除，故不再污染横比（这是从 harness 的
   `_parse_score` 对齐过来的同款夹紧，消除了两个解析器的漂移）。
3. **方差极小是优点也是提醒。** DeepSeek 的 spread < 4 分，远好于 haiku 的 13–16 分，
   K=3 已足够稳。但低方差 + 总分锚定意味着**绝对值不可逐分采信**；基线的价值仍在
   **序 / 区分度 / 独立视角**，而非与 in-loop 逐分对齐。

## 模型决策：为什么从 `haiku[1m]` 切到 `deepseek-chat`

阶段 5.3b 的扰动实验（`scripts/perturbation_harness.py`）是决定性证据：故意破坏论文
（删推导、篡改结果数字）后看评委能否扣分。

| 评委 | 总分扣分检出率 | 维度扣分检出率 |
|---|---:|---:|
| `haiku[1m]` | 0/15 = **0%** | 6/15 = 40% |
| `deepseek-chat` | 11/15 = **73%** | 8/13 = 62% |

haiku 对"被破坏的论文"几乎无动于衷（总分零检出），不能作为可信的质量判别器。
DeepSeek 检出率高一个数量级，且在本机路由上不像 `opus[1m]` 那样卡死（后者对重型评委
请求 300s 0 字节，详见附录 A）。`deepseek-chat` 与流水线的 codex/gpt 谱系不同，
self-preference 偏置的初衷仍成立。模型是 `CLAUDE_MODEL` 一行可覆盖的旋钮：
若有直连 Anthropic key 的端点，可 `CLAUDE_MODEL=opus` 试更强评委。

## 复现

```bash
# 解析器对 in-loop 文件应得 total=80.2/86.4, recomputed=81.8/88.1
python3 scripts/parse_judge_score.py complete/test_cumcm2024a/judge_evaluation.md
python3 scripts/parse_judge_score.py complete/test_cumcm2024b/judge_evaluation.md

# 重跑外部校准（DeepSeek 方差小；序应保持 2024b > 2024a，看 median_recomputed）
JUDGE_TIMEOUT=300 ./evaluation/run_evaluation.sh complete/test_cumcm2024a --samples 3
JUDGE_TIMEOUT=300 ./evaluation/run_evaluation.sh complete/test_cumcm2024b --samples 3
```

---

## 附录 A：haiku 时代基线（已归档，2026-06-02）

下表为切换到 DeepSeek 之前、用 `haiku[1m]` 跑出的第一份外部读数，保留作历史对照。
**不再是活跃基线**——haiku 的扰动检出率为 0%（见"模型决策"），不可信。

| 项目 | 外部中位数 | 外部 spread | n_scored | 偏差(外-内) |
|---|---:|---:|---:|---:|
| `test_cumcm2024a` | 73.0 | 65.0 – 81.0 | 2/3 | −7.2 |
| `test_cumcm2024b` | 94.7 | 86.0 – 99.3 | 3/3 | +8.3 |

haiku 时代的已知问题：单次方差 13–16 分；偶发首行非 `VERDICT:`（2024a 1/3 次无分）；
1 次把灵敏度打成 18.33/10 把总分抬到 99.3（当时解析器不夹紧，现已修复）。
当时的"模型决策"是：`opus[1m]` 在 `anyrouter.top` 路由上对重型评委请求卡死
（300s 0 字节），`sonnet[1m]` 连 PONG 都超时，`haiku[1m]` 是唯一能完成的 Claude 模型
（~40–150s）。这一路由限制依旧成立，故现选 DeepSeek 而非 Claude 大模型。

