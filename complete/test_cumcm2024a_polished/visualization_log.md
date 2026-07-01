# Step 8 Visualization Log — `test_cumcm2024a_polished`

总图数: 15。本文按优秀论文可视化基准把图表分为四类叙事角色：
`explain_model` / `report_result` / `validate_result` / `show_limitation`。
每个子问题均至少保留一个主图或主表作为视觉锚点；稀疏抽样、未采信曲线和辅助最短化解只作为验证或对照材料，不抢占最终答案口径。

## 图清单 + 论文映射 + caption 起草

| 图文件 | 论文章节 | 子问题 | 叙事角色 | 依据来源 | caption 起草 |
|---|---|---|---|---|---|
| `figures/concept_system_overview.pdf` | §问题分析 | 总览 | `explain_model` | `model.md`, `problem/problem_brief.md` | 板凳龙系统结构总览。223 节板凳通过前后把手中心刚性连接成链，全部把手落在顺时针等距螺线上盘入；绿色虚线为半径 4.5 m 的调头圆，放大窗展示龙头附近板凳的刚性矩形实体，用于说明实体碰撞判据不是质点距离判据。 |
| `figures/concept_subproblem_dag.pdf` | §问题分析 | 总览 | `explain_model` | `model.md`, `solve_log.md` | 五个子问题的建模依赖关系。问题 1 建立螺线运动学基础，问题 2 给出无碰撞终止时刻，问题 3 在此基础上搜索推荐保守螺距 0.450 m，问题 4 输出非退化 S 形调头曲线，问题 5 由调头段速度场反解龙头限速。 |
| `figures/solve_p1_inspiral_snapshots.pdf` | §模型求解 §问题1 | problem1 | `report_result` | `results/problem1/values.json` | 问题 1 全队 224 个把手在 t=0,100,200,300 s 的盘入队形。该图展示整队沿顺时针等距螺线向中心收拢、外圈逐步解开的几何过程，是问题 1 位置输出的主视觉锚点。 |
| `figures/solve_p1_speed_profile.pdf` | §模型求解 §问题1 | problem1 | `validate_result` | `results/problem1/values.json` | 问题 1 选定把手速度随时间变化。龙头恒为 1.000 m/s，其余把手速度沿链向后略有衰减但均接近龙头速度，支持“盘入阶段链式速度传递近乎无损”的结论。 |
| `figures/solve_p2_terminal_collision.pdf` | §模型求解 §问题2 | problem2 | `report_result` | `results/problem2/values.json` | 问题 2 盘入终止时刻 t*=412.473838 s 的全队队形。红色实体矩形标出首次接触的题目第 1、9 节板凳，放大窗显示二者恰好相切，是终止时刻判定的主视觉证据。 |
| `figures/solve_p2_clearance_decay.pdf` | §模型求解 §问题2 | problem2 | `validate_result` | `results/problem2/values.json` | 问题 2 全队非相邻板凳最近距离裕度 g(t) 随时间衰减。红色虚线标出 Brent 连续求根得到的首碰时刻，说明本文没有只依赖整数秒采样判碰。 |
| `figures/solve_p3_pitch_geometry.pdf` | §模型求解 §问题3 | problem3 | `validate_result` | `results/problem3/values.json`, `results/sensitivity/*` | 问题 3 稀疏抽样下界 0.4299 m 时龙头抵达调头圆边界的全队队形。该图用于解释最小螺距受后随板凳实体干涉约束；最终推荐值已由加密抽样上调为 0.450 m。 |
| `figures/solve_p3_bisection.pdf` | §模型求解 §问题3 | problem3 | `validate_result` | `results/problem3/values.json` | 问题 3 稀疏抽样下的二分搜索收敛过程。该图展示可行/不可行区间如何收紧，但只作为搜索过程说明；最终提交口径采用 61 点加密认证的 0.450 m。 |
| `figures/solve_p4_scurve_geometry.pdf` | §模型求解 §问题4 | problem4 | `report_result` | `results/problem4/values.json` | 问题 4 非退化 S 形调头曲线几何构造。两段圆弧满足与盘入、盘出螺线相切和半径比 2:1，得到 R1=3.005 m、R2=1.503 m、总长 13.621230 m，并位于半径 4.5 m 调头圆内。 |
| `figures/solve_p4_uturn_snapshots.pdf` | §模型求解 §问题4 | problem4 | `report_result` | `results/problem4/values.json` | 问题 4 全队通过调头区域的三帧队形。该图展示队伍由盘入到盘出的连续过渡，配合正文的 201 帧 SAT 实体扫描零碰撞结论，支撑调头方案物理可执行。 |
| `figures/solve_p5_speed_amplification.pdf` | §模型求解 §问题5 | problem5 | `validate_result` | `results/problem5/values.json`, `results/sensitivity/*` | 问题 5 链式速度放大分析的整数秒稀疏抽样视图。该图说明局部速度峰值来自调头段链式速度放大；最终安全速度需按 0.5/0.25 s 加密步长收敛值 1.258747 m/s 报告。 |
| `figures/sensitivity_oat_p3_pitch_boundary.pdf` | §灵敏度分析 | problem3 | `validate_result` | `sensitivity_report.md`, `results/sensitivity/*` | 问题 3 最小螺距邻域的加密抽样验证。11 点抽样下 0.4297--0.4301 m 邻域整体不可行，说明稀疏抽样下界不能作为最终口径，支持正文推荐 0.450 m。 |
| `figures/sensitivity_oat_p5_timestep.pdf` | §灵敏度分析 | problem5 | `validate_result` | `sensitivity_report.md`, `results/sensitivity/*` | 问题 5 龙头最大速度对扫描步长的收敛性。步长由 1 s 加密到 0.5 s 后速度降至 1.258747 m/s，并在 0.25 s 保持一致，支持采用加密收敛值。 |
| `figures/sensitivity_scenario_p4_rmin_method_compare.pdf` | §灵敏度分析 | problem4 | `validate_result` | `sensitivity_report.md`, `evaluation.md` | 问题 4 转弯半径下界对主调头曲线和辅助非线性规划对照的影响。图中主曲线可执行性由全队无碰撞核验锚定，辅助解贴住 Rmin 并向中心塌缩，不能作为最终路径。 |
| `figures/compare_p4_m1_m2_geometry.pdf` | §模型评价 | problem4 | `show_limitation` | `evaluation.md`, `results/problem4/values.json` | 问题 4 主调头曲线与辅助最短化曲线的几何对照。红色主曲线到达调头圆边界且全队可通过；蓝色辅助解虽短但切点塌缩至中心附近，整链无法物理通过，因此只作为未采信对照。 |

## 给论文起稿与审稿的约束

- `report_result` 图优先放在对应问题的模型求解段落。
- `validate_result` 图用于支撑最终口径可信度，不作为替代最终答案的主图。
- `show_limitation` 图只放在模型评价或局限讨论，不放在每问主答案前。
- caption 必须同时说明图中内容和它支持的结论。

## 已同步的润色口径

- 问题 3 最终口径为 61 点加密抽样认证的 `p*=0.450 m`，`0.4299 m` 仅为稀疏抽样下界。
- 问题 4 主调头曲线为非退化 S 形曲线，`R1=3.005 m`、`R2=1.503 m`、`L=13.621230 m`。
- 问题 5 最终安全速度为加密收敛值 `v0*=1.258747 m/s`，整数秒 `1.443369 m/s` 仅为稀疏采样参考。
