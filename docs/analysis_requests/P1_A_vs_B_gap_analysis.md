# P1 分析请求：A 题 vs B 题的 25 分质量缺口

## 背景

P0 硬指标基线 (`evaluation/hard_metrics_baseline.md`) 揭示了一个重大且未被解释的质量信号：

| 项目 | 评委分数 | symbol_coverage | symbols_undefined |
|---|---:|---:|---:|
| **test_cumcm2024a** | **66.3** | **0.408** | **29** |
| test_cumcm2024b (baseline) | 91.6 | 0.533 | 28 |
| 所有 B 题消融变体 | 86.8-89.9 | 0.652-0.824 | 9-16 |

- **25 分的评委分数差距**（66.3 vs 91.6）是所有已测量差距中最大的
- 远大于任何消融实验的效应量（最大 Δ−4.8）
- 符号覆盖率 0.408 远低于所有 B 题（0.533-0.824）
- symbols_undefined = 29，是所有项目中最高的

## 分析目标

**找出流水线在什么题型/环节上出现结构性失败，导致 A 题质量崩溃。**

具体问题：
1. A 题和 B 题在**问题本质**上有何差异？（离散 vs 连续？评价 vs 优化？数据驱动 vs 建模？）
2. 符号覆盖率 0.408 的根因是什么？（Step 4 符号表构建？Step 10 Gate 1 检查失效？）
3. 其他硬指标的差异模式能否定位到具体 Step？
4. 这个失败是**系统性的**（A 类题都会失败）还是**偶然的**（这个 A 题恰好踩坑）？

## 分析范围

### 主要对比对象
- `complete/test_cumcm2024a`（A 题，66.3 分，symbol_coverage 0.408）
- `complete/test_cumcm2024b`（B 题基线，91.6 分，symbol_coverage 0.533）

### 需要检查的关键文件

**问题理解层（Step 0-1）**：
- `problem/problem_statement.md` — 问题类型、约束、数据特征
- `problem/candidate_methods.md` — 初步方法画像
- `research_brief.md` — 背景研究质量

**方法选择层（Step 2-3）**：
- `viable_streams.md`, `m*_spec.md`, `m*_critique.md` — 提案质量
- `method_decision.md`, `chosen_method.md` — 选型决策

**模型构建层（Step 4，符号覆盖率崩溃的可能根因）**：
- `model.md` — 模型完整性
- `symbol_table.md` — 符号定义覆盖
- `assumption_ledger.md` — 假设保护（A题仅2个PROTECTED，B题基线4个）
- `models/**/*.py` — 代码实现与符号使用

**质量门禁层（Step 10, 13）**：
- `code_review.md` — Gate 1 是否检出符号覆盖问题
- `audit_issue_ledger.md` — 发现的问题是否被解决
- `judge_evaluation.md` — Gate 2 判定

**评委视角**：
- `evaluation/results/test_cumcm2024a_eval.json` — 逐维扣分分布
- `evaluation/results/test_cumcm2024b_eval.json` — 对照

## 预期输出

一份诊断报告 (`evaluation/P1_A_vs_B_gap_diagnosis.md`)，包含：

1. **问题差异画像**  
   - A/B 题的问题本质分类（用建模领域标准分类）
   - 数据类型、约束结构、求解目标的对比

2. **符号覆盖率崩溃的溯源链**  
   - 从 symbol_table.md 的 29 个未定义符号往上溯源
   - 定位到具体哪个 Step 的哪个决策/检查失效了

3. **其他硬指标的差异模式**  
   - PROTECTED 假设数（2 vs 4）的原因
   - 代码足迹差异（19 files/225 lines 均值 vs 15 files/250 均值）

4. **失败模式分类**  
   - 是题型结构性弱点（连续/PDE？评价类？）
   - 还是偶然踩坑（某个 agent 的随机选择？）

5. **可操作的修复方向**  
   - 需要强化哪个 Step 的检查？
   - 需要补充哪类 method_library 条目？
   - 需要调整哪些 prompt 或门禁逻辑？

## 执行建议

**用 auto model 派一个深度分析 agent**，让它：
1. 对比读取上述关键文件
2. 构建从问题→方法→模型→代码的决策链对比
3. 定位符号覆盖率崩溃的具体环节
4. 给出可操作的修复建议

预计这个分析的科学价值 > 跑 10 组 B 题消融，因为它直接暴露流水线的结构性弱点。
