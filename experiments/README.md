# `experiments/` — 消融实验（Ablation Study）

本目录维护消融实验，具体开关和运行方式见下文。逐个关闭流水线的三个"加料"机制（原有的第四个 `no_method_lib` 已退役，见下文），在同一道题上
重跑，用外部评分器（`evaluation/`）测量分数差——把"我们觉得这个组件有用"变成
**数据支撑的证据**，供研究论文使用。

## 三个消融开关

每个开关是 Native 引擎读取的环境变量，默认 OFF（未设或非真值）。真值为
`1` / `true` / `yes` / `on`。prompt 类消融在 **render 时**生效（`factory_core/steps/prompting.py` 的 `_apply_ablations()`），
**不改 prompt 源文件**（它们是 agent 契约，见 `CLAUDE.md`）。

| 环境变量 | 关闭的机制 | 实现位置 |
|---|---|---|
| `ABLATE_NO_CONSULTATION` | Step 1 的 web 文献检索 | `_apply_ablations()` 删掉 `step1_research_viability.txt` 的 web 子句 |
| `ABLATE_NO_JUDGE` | Step 13 Gate-2 评委 + reopen | `factory_core/steps/specialized.py` 写 `VERDICT: PRECHECK_PASS`（Step 13）/ `VERDICT: PASS` stub；`factory_core/audit/service.py` 让最终审计跳过评委 |
| `ABLATE_NO_INNOVATION_PROTECT` | PROTECTED 不可降级规则 | `_apply_ablations()` 删掉各 Step 提示词里的 PROTECTED 强制行 |

事后核对条件：`ABLATE_NO_JUDGE` 会在 `judge_evaluation.md` 写下 `Ablation: automated judge disabled.`，
最终审计记录的 `decision` 为 `ABLATE_NO_JUDGE`；两个提示词类开关不留日志标记，只能从项目名的
`_<tag>_rep<k>` 后缀区分。开关经环境变量传给 `run_paper.sh`，环境原样保留。

## 用法

```bash
# 单个消融，在 B 题上跑 3 次（真实流水线，每次数小时，消耗 codex/agy 配额）
./experiments/ablation_no_judge.sh --problem B --reps 3

# 加跑一个无消融对照组（<problem>_baseline_repK），用于配对比较
./experiments/ablation_no_innovation_protect.sh --problem B --reps 3 --baseline

# 先看要跑什么命令，不创建任何项目、不调用任何 agent
./experiments/ablation_no_consultation.sh --problem A --dry-run

# 复用一个已存在的项目（只设开关跑一次 + 评分，不新建）
./experiments/ablation_no_judge.sh --existing ongoing/foo
```

公共参数（三个 launcher 通用）：

| 参数 | 默认 | 含义 |
|---|---|---|
| `--problem A\|B\|C\|D\|/abs/path.pdf` | `B` | 题目；A–D 映射到 `benchmark/cumcm_2024/<X>题/<X>题.pdf` |
| `--reps N` | `3` | 每个变体重复次数（对抗流水线随机性） |
| `--samples K` | `3` | 传给 `run_evaluation.sh` 的评委采样数 |
| `--baseline` | 关 | 额外跑无消融对照组 |
| `--existing <dir>` | 无 | 复用已有项目，单次运行 |
| `--dry-run` | 关 | 只打印命令，不执行 |

项目命名：`<problem>_<tag>_rep<k>`，例如 `cumcm2024b_no_judge_rep1`、
`cumcm2024b_baseline_rep1`。这些落在 `ongoing/`（gitignored）。

## 对比分数

```bash
# 基线 vs 一个或多个消融变体，打印 total + 6 维度中位数 + Δ
python3 experiments/compare_ablations.py \
    --baseline cumcm2024b_baseline_rep1 \
    --variant  cumcm2024b_no_judge_rep1 \
    --variant  cumcm2024b_no_innov_rep1

# 机器可读
python3 experiments/compare_ablations.py --baseline ... --variant ... --json
```

横比轴是 **`median_recomputed`**（夹紧后六维加权和），不是评委手写的 `整体得分`——
后者会被 DeepSeek 锚定，破坏序（详见 `evaluation/baseline_scores.md`）。
输出会标出每个变体**掉分最多的维度**（`↓most`），即被消融组件原本在保护的能力。
当前基线参考：`evaluation/baseline_scores.md`（deepseek-chat，2024b=70.6 > 2024a=66.3）。

## 解读与重要注意

- **预期方向**：关掉某组件后总分**下降**，说明该组件有用；下降越多越关键。
  例如 `no_innov` 预期最伤"创新性"维度，`no_judge` 预期伤整体一致性。
- **`no_judge` 的混淆**：关闭 Step 13 同时移除了 Gate-2 reopen → Step 12 第二轮修订。
  所以它相对基线的差异**同时包含**"无评委"与"无评委触发的二次修订"两个效应，
  不能纯归因于评委本身。这是 write-PASS 这种 off-switch 的固有特性，已知并记录。
- **`no_method_lib` 已退役**：它关闭的 `check_method_citations()` 引用硬门只存在于 Legacy
  Adapter，Native 主流程没有这个门，`ABLATE_NO_METHOD_LIB` 也不再被读取，因此 launcher
  已于 2026-11-11 删除。`evaluation/` 中的 `no_method_lib` 读数只是历史结果。
- **统计稳健性**：每个变体至少 3 次重复取中位数。单次差异可能被流水线随机性淹没。

## 测试（不跑完整流水线）

- `ABLATE_NO_JUDGE` 由 pytest 覆盖（`tests/test_audit_service.py`、`tests/test_workflow_state.py`、`tests/test_atomic_release.py`）。
- `ABLATE_NO_CONSULTATION` 和 `ABLATE_NO_INNOVATION_PROTECT` 目前没有自动测试；它们在 `factory_core/steps/prompting.py` 里改写提示词。
- 正式运行前，可以给任一 launcher 加 `--dry-run`，只打印将要执行的命令。

原来的 `test_ablations.sh` 是从已删除的 Legacy Adapter（`factory_core/adapters/legacy_runner.sh`）里抽取函数来测的，已于 2026-11-14 删除。

## 文件

| 文件 | 作用 |
|---|---|
| `_ablation_common.sh` | 共享：参数解析 + 创建→运行→评分流程（被三个 launcher source） |
| `ablation_no_consultation.sh` | 设 `ABLATE_NO_CONSULTATION=1` |
| `ablation_no_judge.sh` | 设 `ABLATE_NO_JUDGE=1` |
| `ablation_no_innovation_protect.sh` | 设 `ABLATE_NO_INNOVATION_PROTECT=1` |
| `compare_ablations.py` | 基线 vs 变体的分数差表（复用 `scripts/parse_judge_score.py`） |
