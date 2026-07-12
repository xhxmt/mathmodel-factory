# cumcm_2025_a_blind 偏差修复计划

## 目标

修复 blind 运行诊断出的三类根因（不是"重跑这一题"，而是修工厂机制，使后续所有几何/协同题不再复发）：

- **60% 承诺–实现落差**：`model.md §8.1` 声称的预算/采样/种子被代码打两折且无门禁对账（第三次同类事故）。
- **25% 验证不对称**：所有门禁查可追溯性，零门禁查数学正确性/最优性。
- **10% 判据能力盲点**：agent 自信采用错误的线段–球近似判据，无人对抗质询。`MODELING_CHECKLIST.md:18` 写的其实是正确判据，代码却静默实现了更严的错误变体 → 纯规格–实现分歧。

严格度：新增门禁一律**硬失败**（return 1 卡 Gate 1），与现有 provenance/invariants 门禁同级。

## 复现证据（已用独立脚本验过）

- P1：blind 判据 T=1.362，换精确线段–球相交判据 T=**1.391643**（与范文六位小数一致）；粗网格计数再降到 1.30。
- P4：smoke3(FY3) 单独贡献 **0.000 s**，两窗口间 5.3 s 空洞，静默过 Gate 1。
- P5：15 弹中 **7 弹零贡献**；M3 分 4 槽仅 1 枚有效。
- provenance 门禁 `_MIN_ITERS=50` 且 `_budget_from_values` 优先返回 iters → P2 maxiter=60 过关，50000-eval 下限成死代码；且下限与维度无关（12 维 P4 与 4 维 P2 同阈值）。

---

## A. 机械门禁（安全，不碰 agent 契约）

### A1. `scripts/verify_provenance.py` — 预算下限维度感知 + 修死代码

- `_budget_from_values`：当 provenance.budget 同时含 iters 和 evals 时，**两者都取**并各自校验（现在遇 iters 就 return，evals 下限永不触发）。
- 预算下限按决策维度分级：新增 `n_vars`（或从 decision.smokes 长度推）读取；下限 `max(_MIN_EVALS_BASE, k * n_vars^2)` 量级，低维不误伤、≥12 维强制抬预算。默认经 blind 数据标定：P2(4维,480) 允许，P4(12维,21744)/P5(45+维) 触发 BUDGET_LIMITED。
- 阈值全部走环境变量（`SOLVE_MIN_EVALS`、新增 `SOLVE_EVALS_PER_DIM2`），legacy 项目无 provenance 仍 SKIP。

### A2. `scripts/verify_invariants.py` — 新增两类不变量

纯新增 check 分支，不改现有语义（legacy 无 invariants.json 仍 SKIP）：

- `type: quantized_off`（反网格量化）：断言 canonical objective **不恰为** Δt 的整数倍（`abs(x/dt - round(x/dt)) > eps`）。抓"粗值当 canonical、精化值降级为诊断"。参数 `value`、`dt`、`eps`。
- `type: nonzero_each`（逐资源非零，批量版）：给一个 JSON 数组路径（如 `canonical:p4.per_smoke[].contribution`），断言**每个**元素 > min_abs。现有 `nonzero` 只能查单值，P4/P5 逐弹零贡献需要批量。

> 依赖：Step 5 求解脚本需在 canonical 里落 `per_smoke[].contribution`（逐弹单独遮蔽时长）。这属于 B 类契约改动（见 B2），A2 只提供门禁能力。

### A3. 新增 `scripts/verify_spec_impl.py` — 规格–实现对账门禁（治 60% 根因）

机械对照 `model.md §8.1` 承诺 vs 代码常量，专防"说到做不到"：

- 从 `model.md` 抽承诺数值：预算阶梯（particles×iterations）、Δt、种子数、松弛上界/gap 是否声明。
- 从 `results/p*/values.json` 的 `provenance.budget` 抽实际 n_eval / iters、`time_step_sec`。
- 规则：实际预算 < 承诺预算的 50% → BLOCKING；承诺了 gap/上界但 canonical 无 `mip_gap`/`upper_bound`/`gap` 字段 → BLOCKING；承诺 Δt≤0.05 实跑 0.08 → BLOCKING。
- 输出 `spec_impl_verification.latest.txt`，退出码 0/1；无 model.md §8.1 或无 results 树时 SKIP（legacy 安全）。

### A4. `run_paper.sh` — 接线（快照机制，热改安全）

- 新增 `spec_impl_gate_passed()`（仿 `invariants_gate_passed`，行 137-141 模式）。
- Step 10 (行 1200 附近) 追加：`spec_impl_gate_passed "$P" || return 1`，并对新报告做 `_verification_is_fresh` 检查（行 1205 模式）。
- A1/A2 走现有 `invariants_gate_passed`/`provenance_gate_passed`，无需新接线。

---

## B. Agent 契约（敏感文件，按 CLAUDE.md 谨慎改）

### B1. `docs/guides/MODELING_CHECKLIST.md` — 判据精确化（治 10%）

- 第 18 行"线段 vs 无限直线"条：明确**正确判据是线段–球相交**（判别式 R²−d²≥0 且交点参数 s∈[0,1]，或整段在球内 s₁<0∧s₂>1），并**点名反面案例**："仅用垂足距离 `0<proj<‖L‖ 且 d≤r` 会漏掉垂足在端点外但球仍切割线段末端的情形，系统性低估遮蔽出口边界约 2%"。
- 新增"精度口径"条：canonical 采信值必须由**边界二分精化**（到 1e-3 s），粗网格只准用于搜索定位；采信值恰为 Δt 整数倍是量化未精化的红旗。

### B2. `prompts/step5_full_solve.txt` — canonical 精化 + 逐弹落盘契约

- canonical objective 必须经区间端点二分精化（容差 1e-3），粗网格结果只写诊断字段。
- results 落 `per_smoke[].contribution`（逐资源单独遮蔽时长）供 A2 门禁，及 `mip_gap`/`upper_bound`/`gap` 供 A3 门禁。
- ≥12 维子问题：强制**结构 baseline 对照**——无先验 DE/PSO vs §8.1 承诺的几何引导搜索，两者差 >5% 不得宣称收敛。

### B3. `prompts/step4_model_construction.txt` — §8.1 从"必写"升"必证"

- §8.1（行 126-140）追加：结构分析不得停留在话术，必须附**可运行验证**（判据双实现交叉校验脚本复算 P1 锚点，容差 1e-3；拦截点降维给数值验证）。
- 新增判据自检项：模型定稿前须用精确判据与近似判据在一个固定策略上对比，差异 >1% 须在 §8.1 记录并采用精确判据。

### B4. `STEPS.md` — 契约登记

- Step 5 (行 92-98)：登记 `verify_spec_impl.py` 为 Gate 1 第五道机械门禁；登记 canonical 精化与 `per_smoke[].contribution` 要求。
- Step 10 (行 175)：机械门禁从"四道"改"五道"，说明 spec-impl 与 quantized/nonzero_each 语义与 legacy SKIP 行为。

---

## C. 验证方式

- A1/A2/A3 脚本：先在 `complete/cumcm_2025_a_blind` 上跑（只读，不改该项目），**必须复现**：A1 对 P4/P5 报 BUDGET_LIMITED；A2 quantized_off 对 1.30/4.45/... 报 FAIL、nonzero_each 对 P4 smoke3 报 FAIL；A3 报预算/Δt/gap 落差。这是"门禁能抓已知病"的回归证据。
- 在一个 legacy 项目（如 `cumcm2024b_no_consult_rep1`）上跑三脚本，**必须 SKIP**（无 §8.1 / 无 invariants 新字段），证明不误伤。
- `run_paper.sh` 改动后跑 `bash -n` 语法检查；不触碰运行中 runner（快照隔离）。

## D. 不做

- 不重跑 2025A（是机制修复，不是刷分）。
- 不改判据代码本身进任何 `complete/` 项目（历史存档只读）。
- 不动 P5 求和 vs 交集的口径选择（blind 的求和对题面是合理解读，缺的是双口径展示，属 B1 已覆盖的诊断量报告，不升级为强制）。

## 落地顺序

1. A1 → A2 → A3（纯脚本，各自在 blind 上自证抓病）
2. A4 接线 + `bash -n`
3. B1 → B4（契约文本）
4. C 全量回归（blind 抓病 + legacy SKIP）
