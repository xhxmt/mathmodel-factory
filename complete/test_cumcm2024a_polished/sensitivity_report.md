# Step 6 Sensitivity + Robustness Report — `test_cumcm2024a`

主流程: m1
辅流程: m2
扫描总数: 5; 涉及参数 5 个 / 涉及子问题 4 个
计算预算: Step 5 实测 solver runtime 422.66s = 0.16% of 74h; 本步骤实际 solver runtime 389.38s = 0.15% of 74h; 剩余约 99.69% 总赛时预算留给 reopen 周期与论文整合。若按 `problem/feasibility_constraints.md` 中灵敏度与对照模型 4h 子预算计，本步骤实测占 2.70%。

## 1. 扫描计划（对照 solve_log.md §Step 6 接力）

| sweep_id | 类型 | 参数 / 场景 | 范围 / 取值 | 涉及子问题 | jobid | runtime (s) | 输出 |
|---|---|---|---|---|---|---:|---|
| s1_p2_collision_eps | 单参数扫描 | 碰撞容差 $\varepsilon$ | $\{10^{-8},10^{-7},10^{-6},10^{-5}\}$ | problem2 | local_python_20260530081207_220784 | 34.99 | `results/sensitivity/s1_p2_collision_eps.json` |
| s2_p3_pitch_boundary | 边界加密扫描 | 螺距 $p$ | $[0.4297,0.4301]$，步长 0.00005 | problem3 | local_python_20260530075458_216398 | 255.53 | `results/sensitivity/s2_p3_pitch_boundary.json` |
| s3_p3_collision_samples | 抽样密度扫描 | P3 碰撞抽样密度 | samples $\in \{7,11,15\}$ | problem3 | local_python_20260530075459_216440 | 87.36 | `results/sensitivity/s3_p3_collision_samples.json` |
| s4_p4_rmin_compare | scenario-comparison | $R_{\min}$ 与 m1/m2 对照 | $R_{\min}\in\{1.5,2.0,2.5,3.0\}$ | problem4 | local_python_20260530075500_216467 | 0.31 | `results/sensitivity/s4_p4_rmin_compare.json` |
| s5_p5_dt_sweep | one-at-a-time | 时间步长 $\Delta t$ | $\{1.0,0.5,0.25\}\,\mathrm{s}$ | problem5 | local_python_20260530075501_216498 | 11.19 | `results/sensitivity/s5_p5_dt_sweep.json` |

**计划修订**: `solve_log.md` 的 5 条 sweep 已 100% 覆盖，未新增 sweep。s1 的首次全队全距离二分实现耗时过高，后改为“首碰邻域精定位 + 事件点全链复核”；数值输出仍使用同一矩形距离原语，并在 JSON 中记录 `local_neighborhood`。

## 2. 每条扫描结果

### s1_p2_collision_eps: 碰撞容差 $\varepsilon$

- **设计**: 将 P2 的连续碰撞事件从 $g(t)=0$ 放宽为 $g(t)\le \varepsilon$，测试临界时刻对几何容差的稳定性。
- **运行**: `../../solver_submit.sh --type python --max-time 120 models/m1_spiral/05_sensitivity.py --args "--sweep=s1_p2_collision_eps"`，jobid `local_python_20260530081207_220784`。
- **结果摘要**:

| $\varepsilon$ | $t_\varepsilon$ (s), baseline=412.473838 | $\Delta t$ (s) | 首碰对 | 一致性 |
|---:|---:|---:|---|---|
| $10^{-8}$ | 412.473837 | -0.000001 | (0, 8) | 稳定 |
| $10^{-7}$ | 412.473834 | -0.000004 | (0, 8) | 稳定 |
| $10^{-6}$ | 412.473800 | -0.000038 | (0, 8) | 稳定 |
| $10^{-5}$ | 412.473458 | -0.000380 | (0, 8) | 稳定 |

- **定性结论**: P2 终止时刻对 $\varepsilon$ 不敏感；即使容差放宽到 $10^{-5}$，事件只提前 $3.8\times10^{-4}$s，首碰对保持 (0, 8)。
- **触发的假设**: A3/A4 状态保持 `CONFIRMED`，PROTECTED 标签不动。
- **对论文的影响**: 可在灵敏度章节说明连续碰撞定位不是由数值容差任意决定。

### s2_p3_pitch_boundary: 螺距 $p$ 边界加密

- **设计**: 在 Step 5 的 $p^*=0.429883$ 附近对 $[0.4297,0.4301]$ 做加密扫描，使用更密的 11 点时域碰撞抽样判断边界可行性。
- **运行**: `../../solver_submit.sh --type python --max-time 900 models/m1_spiral/05_sensitivity.py --args "--sweep=s2_p3_pitch_boundary"`，jobid `local_python_20260530075458_216398`。
- **结果摘要**:

| $p$ (m) | 与 baseline 差值 (m) | 可行性 | 最小 $g(t)$ | worst pair |
|---:|---:|---|---:|---|
| 0.429700 | -0.000183 | 不可行 | 0.000000 | (0, 20) |
| 0.429750 | -0.000133 | 不可行 | 0.000000 | (0, 20) |
| 0.429800 | -0.000083 | 不可行 | 0.000000 | (0, 20) |
| 0.429850 | -0.000033 | 不可行 | 0.000000 | (0, 20) |
| 0.429900 | +0.000017 | 不可行 | 0.000000 | (0, 20) |
| 0.429950 | +0.000067 | 不可行 | 0.000000 | (0, 20) |
| 0.430000 | +0.000117 | 不可行 | 0.000000 | (0, 20) |
| 0.430050 | +0.000167 | 不可行 | 0.000000 | (0, 20) |
| 0.430100 | +0.000217 | 不可行 | 0.000000 | (0, 20) |

- **定性结论**: 在更密抽样口径下，原 Step 5 边界邻域整体不可行；这不是 $p$ 的微小漂移，而是 P3 充分条件验证口径翻转。
- **触发的假设**: A3/A4 为 PROTECTED，状态不放松；翻转作为抽样充分性风险写入 §定性翻转记录。
- **对论文的影响**: 不应直接把 $p=0.429883$ 写成无条件稳健结论；论文需披露“最小螺距受碰撞抽样密度约束，保守值应上调或在后续 reopen 中重算”。

### s3_p3_collision_samples: P3 碰撞抽样密度

- **设计**: 固定 Step 5 的 $p^*=0.429883$，只改变到达边界过程中的碰撞检查样本数。
- **运行**: `../../solver_submit.sh --type python --max-time 900 models/m1_spiral/05_sensitivity.py --args "--sweep=s3_p3_collision_samples"`，jobid `local_python_20260530075459_216440`。
- **结果摘要**:

| samples | 可行性 | 最小 $g(t)$ | worst pair | 与 baseline 一致性 |
|---:|---|---:|---|---|
| 7 | 可行 | 0.000026 | (0, 20) | 一致 |
| 11 | 不可行 | 0.000000 | (0, 20) | 翻转 |
| 15 | 不可行 | 0.000000 | (0, 21) | 翻转 |

- **定性结论**: P3 的可行性判断受抽样密度控制；7 点抽样漏过了 11/15 点抽样捕获的碰撞。
- **触发的假设**: A3/A4 PROTECTED 状态不动；该结果说明实现层的充分性检查需要在后续 reopen 中采用更密抽样或连续事件定位。
- **对论文的影响**: 灵敏度章节必须明示 P3 最小螺距是当前最脆弱结论。

### s4_p4_rmin_compare: $R_{\min}$ 与 m1/m2 对照

- **设计**: 比较 m1 边界锚定 S 曲线与 m2 NLP lower-bound proxy 在不同 $R_{\min}$ 下的路径长度和可行状态。
- **运行**: `../../solver_submit.sh --type python --max-time 900 models/m1_spiral/05_sensitivity.py --args "--sweep=s4_p4_rmin_compare"`，jobid `local_python_20260530075500_216467`。
- **结果摘要**:

| $R_{\min}$ | m1 状态 | m1 路长 (m) | m2 状态 | m2 路长 (m) |
|---:|---|---:|---|---:|
| 1.5 | FEASIBLE | 13.965378 | OPTIMAL | 1.598097 |
| 2.0 | FEASIBLE | 13.965378 | OPTIMAL | 2.130796 |
| 2.5 | BOUND_EXCEEDS_M1_RADIUS | NA | OPTIMAL | 2.663494 |
| 3.0 | BOUND_EXCEEDS_M1_RADIUS | NA | OPTIMAL | 3.196193 |

- **定性结论**: m1 的几何曲线半径 $R=2.251353$，因此 $R_{\min}\ge2.5$ 时主曲线不再满足下界；m2 fresh 对照持续贴住 $R_{\min}$，说明 A5 是 P4 结论的主导假设。
- **触发的假设**: A5 状态 `OPEN` -> `RELAXED`，CRITICAL 标签不动。
- **对论文的影响**: P4 不能只报告单一最短路长；必须把 $R_{\min}$ 作为外生物理下界披露。

### s5_p5_dt_sweep: 时间步长 $\Delta t$

- **设计**: 固定 P4 路径，将速度倍率扫描从整数秒加密到 0.5s 和 0.25s，检查最大速度倍率是否被整数秒采样低估。
- **运行**: `../../solver_submit.sh --type python --max-time 900 models/m1_spiral/05_sensitivity.py --args "--sweep=s5_p5_dt_sweep"`，jobid `local_python_20260530075501_216498`。
- **结果摘要**:

| $\Delta t$ (s) | 最大速度倍率 | $v_0^*$ (m/s), baseline=1.818058 | 最大点 $(t,i)$ | 相对变化 |
|---:|---:|---:|---|---:|
| 1.00 | 1.100075 | 1.818058 | (34.00, 12) | -0.000016% |
| 0.50 | 1.151139 | 1.737409 | (17.50, 2) | -4.435989% |
| 0.25 | 1.348807 | 1.482792 | (15.25, 1) | -18.440870% |

- **定性结论**: P5 的整数秒扫描明显低估最大速度倍率；若按 0.25s 加密扫描，安全龙头速度上限应降到约 $1.483\,\mathrm{m/s}$。
- **触发的假设**: A1 的等速驱动假设不变；翻转来自速度极值采样精度，不来自物理驱动假设本身。
- **对论文的影响**: 论文若报告安全速度，建议优先引用 0.25s 的保守值，或明确说明 Step 5 整数秒结果是乐观上界。

## 3. 关键 figures

- `figures/sensitivity_oat_p3_pitch_boundary.pdf` — one-at-a-time 图（s2）。横轴为 $p-p^*$，纵轴为最小 sampled $g(t)$；红色表示不可行、蓝色表示可行。
- `figures/sensitivity_scenario_p4_rmin_method_compare.pdf` — scenario-comparison 图（s4）。横轴为 $R_{\min}$，柱形对比 m1 边界锚定 S 曲线与 m2 NLP 对照。
- `figures/sensitivity_oat_p5_timestep.pdf` — one-at-a-time 图（s5）。横轴为 $\Delta t$，纵轴为允许龙头速度 $v_0^*$。

上述图按项目内 `modeling_guide.md §Figure Style` 生成：540×324 pt，白底，Deep blue `#2E5C8A` / Brick red `#C04D4D` / Light gray grid。说明性 note 不嵌入图内，后续在 LaTeX caption 或 `\note{}` 中补。

## 4. 定性翻转记录

**定性翻转**指参数扰动后最优/可行决策改变，或关键安全上界出现数量级上不可忽略的收缩。

- **P3 螺距边界翻转**: 在 11 点抽样口径下，$p\in[0.4297,0.4301]$ 全部不可行；baseline $p^*=0.429883$ 落入该不可行带。A3/A4 为 PROTECTED，不放松，但论文必须披露碰撞抽样充分性风险。
- **P3 抽样密度翻转**: samples 从 7 增至 11/15 后，$p^*=0.429883$ 从可行变为不可行，worst pair 为 (0,20)/(0,21)。建议后续 reopen 采用连续碰撞事件定位或更密网格重算 $p^*$。
- **P4 $R_{\min}$ 场景翻转**: $R_{\min}=2.5,3.0$ 时 m1 曲线不满足半径下界；A5 从 `OPEN` 改为 `RELAXED`。
- **P5 时间步长翻转**: $\Delta t=0.25$s 时 $v_0^*$ 从 1.818058 降到 1.482792，且极值位置从 $(34.0,12)$ 变为 $(15.25,1)$。该翻转属于数值采样风险，应在灵敏度分析中作为保守安全结论呈现。

未发现翻转: P2 碰撞容差 $\varepsilon\le10^{-5}$ 未改变首碰对或终止时刻量级。

## 5. 假设登记簿状态变更（同步 assumption_ledger.md）

- A5: 状态 `OPEN` -> `RELAXED`; 触发的 sweep: s4_p4_rmin_compare。$R_{\min}$ 变化直接改变 P4 主曲线可行性，说明该假设不是稳健外生常量。
- A3/A4: 状态保持 `CONFIRMED`，PROTECTED 标签不动；触发的 sweep: s1_p2_collision_eps, s2_p3_pitch_boundary, s3_p3_collision_samples。P2 容差稳定，但 P3 抽样密度暴露实现层充分性风险。
- A1: 状态保持 `CONFIRMED`; 触发的 sweep: s5_p5_dt_sweep。等速驱动假设未被推翻，但速度极值采样需要采用更保守时间步长。

## 6. 后续步骤接力

- **Step 7 (模型评价)**: 直接引用 §4 的三类风险，不要把 P3/P5 写成完全稳健。
- **Step 8 (图表精修)**: `figures/sensitivity_*.{pdf,png}` 已按项目内 Figure Style 生成；只需补自含 caption / note。
- **Step 9 (论文起稿)**: 灵敏度章节优先使用 P2 稳定性、P3 抽样风险、P4 $R_{\min}$ 对照、P5 保守速度上限四个结论。
