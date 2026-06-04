# `experiments/` — 消融实验（Ablation Study，阶段 5.4）

对照 `docs/refactor_plan.md §5.4`。逐个关闭流水线的四个"加料"机制，在同一道题上
重跑，用外部评分器（`evaluation/`）测量分数差——把"我们觉得这个组件有用"变成
**数据支撑的证据**，供研究论文（§5.5）使用。

## 四个消融开关

每个开关是一个 `run_paper.sh` 读取的环境变量，默认 OFF（未设或非真值）。真值为
`1` / `true` / `yes` / `on`。prompt 类消融在 **render 时**生效（`render_prompt()`），
**不改 prompt 源文件**（它们是 agent 契约，见 `CLAUDE.md`）。

| 环境变量 | 关闭的机制 | 实现位置 |
|---|---|---|
| `ABLATE_NO_CONSULTATION` | Step 1 的 web 文献检索 | `render_prompt()` 删掉 `step1_research_viability.txt:133` 的 web 子句 |
| `ABLATE_NO_METHOD_LIB` | HMML-lite 方法库引用硬门 | `check_method_citations()`（`run_paper.sh:116`）提前 `return 0` |
| `ABLATE_NO_JUDGE` | Step 13 Gate-2 评委 + reopen | `run_step_13()`（`run_paper.sh`）写 `VERDICT: PASS` stub，跳过真实评委 |
| `ABLATE_NO_INNOVATION_PROTECT` | PROTECTED 不可降级规则 | `render_prompt()` 删掉 Steps 4/6/7/10/11/12/13/14 的 PROTECTED 强制行 |

开关生效时，runner 启动日志会打印 `ABLATIONS ACTIVE: ...`（写入项目 `runner.log`），
便于事后核对每个项目跑的是哪个条件。开关经 `export` 传入，能穿过 run_paper.sh 的
snapshot re-exec（`env` 不带 `-i`，环境保留）。

## 用法

```bash
# 单个消融，在 B 题上跑 3 次（真实流水线，每次数小时，消耗 codex/agy 配额）
./experiments/ablation_no_judge.sh --problem B --reps 3

# 加跑一个无消融对照组（<problem>_baseline_repK），用于配对比较
./experiments/ablation_no_innovation_protect.sh --problem B --reps 3 --baseline

# 先看要跑什么命令，不创建任何项目、不调用任何 agent
./experiments/ablation_no_method_lib.sh --problem A --dry-run

# 复用一个已存在的项目（只设开关跑一次 + 评分，不新建）
./experiments/ablation_no_judge.sh --existing ongoing/foo
```

公共参数（四个 launcher 通用）：

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
- **`no_method_lib` 的边界**：开关关闭的是**程序性**引用硬门（真正的强制）。Step 0
  的 prompt 不经 `render_prompt`，仍会软性建议只用已注册方法；故这是软约束而非硬约束的消融。
- **统计稳健性**：每个变体至少 3 次重复取中位数。单次差异可能被流水线随机性淹没。

## 测试（不跑完整流水线）

`test_ablations.sh` 验证四个开关确实改变行为，秒级完成，不调用任何 agent：
语法检查、方法库门翻转、render_prompt 删除（ON 删掉 / OFF 保留双向断言）、
no-judge stub 契约、compare 脚本冒烟。

```bash
./experiments/test_ablations.sh
```

## 文件

| 文件 | 作用 |
|---|---|
| `_ablation_common.sh` | 共享：参数解析 + 创建→运行→评分流程（被四个 launcher source） |
| `ablation_no_consultation.sh` | 设 `ABLATE_NO_CONSULTATION=1` |
| `ablation_no_method_lib.sh` | 设 `ABLATE_NO_METHOD_LIB=1` |
| `ablation_no_judge.sh` | 设 `ABLATE_NO_JUDGE=1` |
| `ablation_no_innovation_protect.sh` | 设 `ABLATE_NO_INNOVATION_PROTECT=1` |
| `compare_ablations.py` | 基线 vs 变体的分数差表（复用 `scripts/parse_judge_score.py`） |
| `test_ablations.sh` | 开关生效性测试 |
