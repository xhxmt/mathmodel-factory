# 最近运行与优秀范文对比诊断

日期: 2026-06-24

## 结论摘要

最近几次运行的核心问题不是单纯出在论文润色，而是出在两层:

1. **建模定型与求解实现层**: 2025A 项目的关键建模口径偏离优秀范文，主要是遮蔽判据使用"任一点遮蔽"而不是"整体/全部点遮蔽"，问题 5 目标函数使用分导弹遮蔽时长求和而不是三枚导弹共同遮蔽区间的交集。这两个问题分别应在 **Step 4 模型构建** 和 **Step 5 求解实现** 阶段被拦截。
2. **交付门禁层**: `cumcm_2025_a_v2` 最终进入 `complete/` 且 `--infer-step` 为 16，但 `judge_evaluation.md` 仍为 `REOPEN_REVISION_TEXT`。runner 当前允许 Gate 2 reopen 一次后继续进入 Step 14/16，且 Step 16 只要求最终 PDF 存在。这说明 **Step 13/16 的完成门禁没有要求最终 Gate 2 PASS，也没有要求 BLOCKING/DEFERRED 问题清零**。

形式层面需要更新: 最终 `cumcm_2025_a_v2` 的正文和 Excel 已补入策略表，并且 `result1.xlsx/result2.xlsx/result3.xlsx` 当前读数与论文口径表面一致。因此旧评审里的"策略表完全缺失、Excel 当前仍错位"不能作为最终状态直接引用。剩余硬问题是 canonical result 锚点不同步、核心模型口径偏离优秀范文、以及 Gate 2 未 PASS 仍交付。

## 当前证据

### 最近完成项目

`complete/` 中最新项目:

| 项目 | 完成时间 | inferred step | 关键状态 |
|---|---:|---:|---|
| `cumcm_2025_a_v2` | 2026-06-23 04:14 | 16 | `judge_evaluation.md` 仍为 `REOPEN_REVISION_TEXT` |
| `cumcm_2025_a` | 2026-06-21 08:59 | 16 | Gate 2 PASS, 但历史总结记录 v1 漏掉 blocking 问题 |
| `cumcm2024b_no_innov_rep1` | 2026-06-09 02:25 | 16 | PASS |
| `cumcm2024b_no_consult_rep1` | 2026-06-07 04:58 | 16 | PASS |
| `cumcm2024b_no_judge_rep1` | 2026-06-05 08:16 | 16 | judge ablation stub, 不能作真实质量信号 |
| `cumcm2024b_no_methodlib_rep1` | 2026-06-05 07:19 | 16 | PASS, 但方法库缺失导致质量下降 |

### `cumcm_2025_a_v2` 的最终文件状态

已核对最终 LaTeX 与 Excel:

- 论文正文已经包含 P2/P3/P4/P5 策略表。
- `result1.xlsx` 总遮蔽时长为 4.90 s。
- `result2.xlsx` 总遮蔽时长为 16.40 s。
- `result3.xlsx` 总遮蔽时长为 14.804 s。

但 canonical 数据源仍有断裂:

- `results/m1_p4_result.json` 的 objective 是 13.10 s。
- `results/p4/values.json` 仍标记为 `RUNNING`。
- `results/p3/values.json` 仍标记为 `PARTIAL`。
- 未发现 `results/p5/values.json`。
- `code_review.md` 已把 P5 缺少 canonical JSON/log 锚点列为 warning，但 Gate 1 仍 PASS。

这说明后续步骤把论文和 Excel 修到了同一个口径，但没有把 authoritative results 树同步为同一真相源。

## 与优秀范文的关键差距

### 1. 遮蔽判据口径

优秀范文采用的是"整个圆柱体被完全遮蔽"口径，工程实现上用上下底面圆周采样并检查全部点遮蔽。

当前项目论文明确写的是:

> 当云团遮挡导弹到真目标任一采样点的视线时即判定为有效遮蔽。

这会系统性高估遮蔽时长。`GEMINI_VS_EXCELLENT_VS_CURRENT.md` 已把三种方案对比为:

| 方案 | 遮蔽判据 |
|---|---|
| Gemini Deep Think | 全部点遮蔽 |
| 优秀范文 | 全部点遮蔽 |
| 当前项目 | 任一点遮蔽 |

根因定位: **Step 4**。遮蔽判据属于模型定义，不是 Step 12 文本修订能真正修复的问题。Step 12 只能解释"任一点遮蔽"的合理性，不能把已完成的求解结果改成优秀范文口径。

### 2. 问题 5 目标函数

优秀范文和 Gemini 方案都把问题 5 理解为三枚导弹同时被遮蔽的共同时间，即区间交集或 `max min(T_M1,T_M2,T_M3)`。

当前项目论文和 Excel 采用的是分导弹遮蔽时长求和:

| M1 | M2 | M3 | 总计 |
|---:|---:|---:|---:|
| 9.520 s | 3.100 s | 2.184 s | 14.804 s |

这会把不同时段分别遮蔽不同导弹的效果相加，无法保证任一时刻三枚导弹都被遮蔽。对防空任务而言，这个目标函数偏离题意。

根因定位: **Step 4 + Step 5**。Step 4 没有把 P5 目标函数定为三导弹遮蔽区间交集；Step 5 的 m3/MILP 分配实现沿用了求和收益。

### 3. 问题 3 多弹优化

优秀范文问题 3 结果约 6.93586 s，明显高于单弹问题 2。当前项目问题 3 最优仍为 4.90 s，且最终策略表显示只有第一枚弹有效，第二、三枚弹有效时长为 0。

根因定位: **Step 5**。P3 的 8 维 PSO/SLSQP 在非光滑区间并集目标下陷入局部峰；Step 4 咨询虽指出高维风险，但没有强制改用更适合该题的差分进化、多起点搜索或热启动候选库。

### 4. 问题 4 数值可信度

优秀范文问题 4 约 11.125936 s。当前论文给出 16.40 s，比范文高约 47%。在当前项目又采用更宽松的"任一点遮蔽"判据时，这个更高数值不能直接视作更优，反而提示需要重审判据和结果来源。

根因定位: **Step 5 + Step 10**。P4 最终论文值来自日志/后修复表格，而 canonical `results/m1_p4_result.json` 是 13.10 s、`results/p4/values.json` 仍为 RUNNING。Step 10 需要从"论文数字 spot check"升级为"论文、Excel、canonical JSON/log 三向一致性"。

## workflow 层问题定位

`run_paper.sh` 当前有三个关键缺口:

1. Step 13 完成判断只要求 `judge_evaluation.md` 存在且包含 `VERDICT:`，不要求 verdict 为 `PASS`。
2. 如果 reopen 后再次得到 `REOPEN_REVISION_TEXT` 或 `REOPEN_REVISION_MODEL`，runner 会按策略继续进入 Step 14。
3. Step 16 只校验最终 PDF 大小，没有校验 Gate 2 最终 PASS、audit ledger blocking 清零、Excel/JSON/论文一致性或提交包内容一致性。

因此 `cumcm_2025_a_v2` 可以在仍然有 Gate 2 reopen 结论时完成交付。

## 修改建议

### P0: 先修交付门禁

1. **Step 13 语义校验**  
   `verify_step_output 13` 不应只检查 `VERDICT:`，应解析 verdict。若为 `REOPEN_REVISION_TEXT` 或 `REOPEN_REVISION_MODEL`，只能回到修订/模型步骤，不能被视作 Step 13 完成。

2. **取消或收紧 "reopen once then proceed" 策略**  
   如果确实需要防止无限循环，也应把项目标为 blocked/incomplete，而不是进入 Step 14/16。至少要求第二次 Gate 2 若仍 reopen，则停止并输出未解决 issue 清单。

3. **Step 16 增加硬验收**  
   Step 16 必须检查:
   - `judge_evaluation.md` 最终 verdict 为 `PASS`。
   - `audit_issue_ledger.md` 中无 `BLOCKING | OPEN/DEFERRED`。
   - 论文表格、Excel、`results/**/values.json` 或对应 logs 三向一致。
   - P5 必须有 canonical `results/p5/values.json` 或等价可追溯锚点。

### P0: 先修 2025A 建模核心

1. **Step 4 强制优秀范文口径对齐检查**  
   对遮蔽类题目增加 checklist:
   - 是否要求全部点/整体遮蔽，而不是任一点遮蔽？
   - 如果使用部分遮蔽，是否有题面或文献支持？
   - 是否做了整体遮蔽阈值对比实验，如 50%、75%、100% 采样点遮蔽？

2. **P5 目标函数改为共同遮蔽区间交集**  
   对 M1/M2/M3 分别得到遮蔽区间集合，目标应为三者交集长度，而不是分导弹时长求和。论文、m3 MILP、Excel `result3.xlsx` 都要同源更新。

3. **P3/P4 改用更稳的全局优化方案**  
   对 8 维以上的非凸连续搜索，默认用差分进化/多起点变步长/热启动候选库，而不是单纯 PSO+SLSQP。P3 至少要证明三弹结果不低于单弹，并解释无增益的情形。

### P1: 修数据源与审查

1. 建立 `results/canonical_results.json` 或每题 `results/pN/values.json` 为唯一数据源。论文表格和 Excel 均由同一脚本生成。
2. Step 10 增加三向一致性检查: `paper.tex` 表格数字、`result*.xlsx`、canonical JSON/log 必须一致。
3. `verify_numbers.py` 的 manifest 应纳入 `results/sensitivity/*.json`、`results/m1_p*_result.json`、`logs/*.log`，减少人工 spot check。

### P1: 把优秀范文对齐固化到 Step 4 prompt

把 `docs/guides/MODELING_CORRECTIONS_GUIDE.md` 中的三条硬约束写入 Step 4:

- 遮蔽判据: 默认整体遮蔽，采样密度 100-300 点或给出误差证明。
- 问题 4: 结果若偏离优秀范文基准超过 20%，必须解释差异来自题意口径、数据还是算法。
- 问题 5: 默认共同遮蔽区间交集，禁止把多导弹时长简单求和当作主目标。

## 对"在哪一步出问题"的最终判断

按影响大小排序:

1. **Step 4 模型构建**: 最大问题。遮蔽判据和 P5 目标函数定错/定宽松，决定了后续所有数值口径。
2. **Step 5 求解实现**: P3 高维优化未获得多弹增益，P4/P5 canonical 结果没有完整落盘。
3. **Step 10 数值 Gate 1**: 没有把论文、Excel、JSON/log 作为三向同源一致性硬门禁。
4. **Step 13/16 交付门禁**: Gate 2 仍 `REOPEN_REVISION_TEXT` 的项目被允许进入完成态，这是 workflow 级缺陷。

一句话: 最近运行的问题不是"最后写得不够像优秀范文"，而是 **Step 4/5 形成了与优秀范文不同的数学问题，Step 10/13/16 又没有把这种差异作为阻断性错误拦住**。
