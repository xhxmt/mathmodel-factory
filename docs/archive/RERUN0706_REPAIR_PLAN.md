# 完整修复方案 — 基于 cumcm_2025_a_rerun_0706 事故链

日期: 2026-07-07
依据: rerun_0706 与优秀论文 A2022729 对比诊断 + 日志/代码级取证。

## 0. 事故链回顾（修什么要先知道坏在哪）

```
[L1 通道] model_config.json 钉死 step_5 primary='claude' fallback='<none>'
          → unity2 代理返回寒暄假会话 ×2（"Hi! I'm Kiro..." 59B/7s）+ 空输出 ×1
          → runner 只认 exit code, verify 才发现无产物
[L2 回退] Codex 因 "gpt-5.5 requires newer Codex" 版本漂移 5 连败
          → fallback 链全灭, 最后的 Claude 兜底会话接手
[L3 兜底] 兜底 agent 目标 = 过门禁, 写出 step5_repair_solve.py
          → 初版 de-maxiter=3/pso-iters=2; m3 MILP 永为 .stub
          → P3 = 3 种子+Nelder-Mead 局部精修(其一硬编码), P5 = 零优化拼装
[L4 门禁] Gate 1/invariants/Gate 2 只验自洽性不验"来自真求解"
          → P3≥P2 以等号通过; invariants 验证 mtime 早于最终 results
          → 5.60/15.70 s 交付, 距范文 -19%/-25%
[L5 污染] MODELING_CORRECTIONS_GUIDE(含当题范文逐题答案)被 agent 引用
          → modeling_scope_gate 直接对答案打 PASS, 全程非盲测
```

五层各自独立可修。优先级: L1/L2 是复发概率最高的（贯穿 rerun 全程，Step 12/13/14
也在 fallback），L3/L4 是质量失真的直接原因，L5 决定 benchmark 成绩是否可信。

---

## P0-A 通道健康（L1）— run_paper.sh

### A1. worker 输出有效性检查（寒暄假会话检测）

位置: `run_claude_worker()`（run_paper.sh:2291 附近）、`run_codex`、`run_agy` 的
返回路径统一加 `_worker_output_valid <log> <elapsed>`：

- log 大小 < 2048 字节 且 运行时长 < 120s → FAIL（本次两个假会话 59B/7s、61B/60s 全中）
- log 中无任何工具调用痕迹（按各 CLI 的 trace 特征 grep，claude 为 `logs/` trace 文件
  mtime 变化，已有 `find_claude_trace_mtime` 可复用）且步骤产物 mtime 无变化 → FAIL
- 命中 FAIL 记 `INVALID_SESSION`，**立即在同一 attempt 内重试一次**（不消耗 5 次
  attempt 预算），连续 2 次 INVALID_SESSION → 该 backend 本次运行拉黑并走 fallback。

验收: 用一个 echo 假 CLI 单测；重放 rerun 的 59 字节 log 应判 FAIL。

### A2. backend canary 预检

runner 启动时（及 `launch_agents.sh status`）对 registry 中 enabled 的每个 backend
发一条 canary（"reply exactly: PONG"，timeout 60s）：

- claude: 能抓出 unity2 路由错乱（回寒暄不回 PONG）
- codex: 能抓出 "requires newer Codex" 版本漂移（本次 5 连败在正式任务里才暴露）
- 结果写 `run_state/backend_health.json`；不健康的 backend 在 dispatch 时跳过并 log。

### A3. 禁止关键步骤单点配置

`get_step_model_ids` 后校验: Step 4/5/9/12 若 primary 配置存在且 fallback 为空，
log WARNING 并把内置 default_fn 显式视为二级 fallback（现状已隐式如此，
但 INVALID_SESSION 快速失败后要保证仍能到达 default_fn，而不是像本次一样
耗尽 8 小时才轮到）。

---

## P0-B 兜底语义（L3）— 让"修复"无法冒充"求解"

### B1. REPAIR/降级产物强制标记 + consult

约定: canonical `values.json` 增加必填字段 `provenance`:
`{"solver": "...", "budget": {...}, "job_id": "...", "repair": bool}`。

- `scripts/verify_solver.py`（已存在）扩展: 校验每个 `results/p*/values.json` 的
  provenance.job_id 能在 `run_state/solver_jobs/` 对账，缺失或 `repair: true` →
  在 Step 5 verify 输出 `REPAIR_FALLBACK` 状态。
- `REPAIR_FALLBACK` 不阻断（避免死锁），但: ① 写入 `audit_issue_ledger.md` 为
  BLOCKING; ② 若 `consultation/enabled` 存在则触发 dynamic gate; ③ Step 16 硬验收
  要求 BLOCKING 清零（见 D3），从而 repair 结果必须在 Step 12 前被真求解替换或
  人工豁免。

### B2. 求解预算下限

Step 5 prompt（prompts/step5_full_solve.txt）+ verify 双侧约束:

- 全局搜索类求解器最低预算写进 prompt 契约: DE maxiter ≥ 50 或等价评估次数
  ≥ 5×10^4 / 子问题；低于下限的 canonical 结果 status 只能写 `BUDGET_LIMITED`。
- `verify_solver.py` 解析 provenance.budget，`BUDGET_LIMITED` 视同 REPAIR_FALLBACK
  进 ledger。（本次初版 de-maxiter=3 会被直接拦下。）

### B3. 设计-实现一致性（stub 检测）

`verify_step_output 5`（run_paper.sh:1053 的 case 增加 `5)` 分支，当前根本没有）:

- `solve_log.md` 存在且 ≥ 20 行（从 infer_step 复制既有标准）
- `results/canonical_results.json` + 每个子问题 `values.json` 存在
- **`model.md` 声明的每个 stream 目录下无 `.stub` 残留**:
  `find models/ -name "*.stub"` 非空 → return 1
- `verify_solver.py` 对账通过（B1）

本次 m3_milp 永为 stub、"P5 唯一可行路径"从未存在的断裂即被此拦截。

---

## P0-C 内生质量门禁（L4）— 不用当题答案也能抓平庸

原则（用户裁定）: **运行时任何 agent 可读路径禁止出现当题范文答案。**
以下检查全部由题目结构内生，实赛可用。

### C1. 严格增益不变量

`scripts/verify_invariants.py` 增加类型 `gt_strict`（带 margin），Step 5 prompt
要求对资源支配关系用严格版:

- P3(三弹) > P2(单弹) + 0.1s。等号 = 搜索退化警报（本次 legacy_shift 4.53=4.53
  恰是三弹退化成单弹的实锤，却以 `ge` PASS）。
- 零贡献资源检查: 任何 `T_single_s == 0` 或 per-missile union == 0 的已部署资源
  → 不变量 FAIL，要求重搜或在 solve_log §修订决策 给出书面论证后人工豁免。

### C2. 预算阶梯 + 边际收益停机

Step 5 契约: canonical 求解跑预算递增序列（如 budget × 1, 3, 9），把
`(budget, objective)` 曲线落盘到 `results/p*/convergence.json`；
相邻两级改进 < 1% 才允许 status=FEASIBLE 定稿，否则 BUDGET_LIMITED。
verify_solver.py 校验 convergence.json 存在且满足停机条件。

### C3. 松弛上界自报 gap

每个优化子问题要求给一个可计算的宽松上界（去耦合约束的独立最优和、资源寿命
上界等），写入 values.json `upper_bound_s` 与 `gap_pct`。gap > 40% 时 Step 7
evaluation 必须解释。评估时无需外部答案即可看出 P5 15.7/17.1 这类余量。

### C4. 验证时效性

`invariants_gate_passed` / `number_gate_passed` 等（verify_step_output 10 所引）
增加 mtime 检查: 验证输出文件必须晚于 `results/**` 中最新的输入文件，
否则视为未验证并自动重跑对应 verify 脚本。（本次 invariants 验证 07:52 早于
10:18 的 repair 重跑，属静默过期。）

---

## P1-D 交付与流程门禁收紧

### D1. Step 13 语义校验
`verify_step_output 13` 从 `grep "^VERDICT:"` 升级为解析 verdict 值；
REOPEN_* 不算 Step 13 完成，只能路由回 Step 12 / Step 4。

### D2. reopen 二次触发 → blocked
第二次仍 REOPEN → 项目标记 `.blocked`，`status` 显示 BLOCKED(step13)，
不再"按策略继续"。宁可停线不可带病交付。

### D3. Step 16 硬验收
- Gate 2 最终 verdict == PASS
- `audit_issue_ledger.md` 无 BLOCKING|OPEN/DEFERRED（含 B1 的 REPAIR_FALLBACK）
- 论文表格 / result*.xlsx / canonical JSON 三向一致（复用 verify_numbers 链）
- models/ 下无 .stub

### D4. 论文-元数据对表
Step 9/15 prompt 增加: 算法描述（代数、种群、采样密度）必须引用
provenance/convergence.json 的实际值。（本次"DE 粗搜(3 代)"是初版真实值、
升级到 45 后未同步，属同类漂移。）

---

## P1-E 信息卫生（L5）— 答案隔离

### E1. 运行时隔离当题答案 — 已落地
- `docs/guides/MODELING_CORRECTIONS_GUIDE.md` → `evaluation/answer_keys/MODELING_CORRECTIONS_GUIDE_2025A.md`
  （render_prompt 不可达），文件顶部加 ANSWER KEY 禁读横幅。
- 新建 `docs/guides/MODELING_CHECKLIST.md`（方法论版，零本题答案），step4 prompt 第 12 项
  改指向它；scope_gate 里"偏离优秀论文基准>20%"一行替换为三条内生检查（gt_strict 增益 /
  松弛上界 gap / 结构降维）。
- preamble（common_prompt_preamble）加"ANSWER-KEY ISOLATION"段：禁止读/搜/引用
  evaluation/、external/、benchmark/、reference_papers/、*answer_key*/*excellent* 及本题
  过往解，一切上界/目标从题面结构推导。
- 已 grep 确认 prompts/ + run_paper.sh + STEPS.md 无残留 live 引用。

### E2. 修订 guide 本身
P5 条目改为: 范文摘要"取交集 21.077s"系行文不严谨（数学上交集 ≤ 最小单导弹
并集），实义为三子问题时长之和；正式目标 = 逐导弹并集之和，同时遮蔽交集作
诊断指标。预期区间只可用于赛后离线评估。

### E3. 方法论固化（合法泛化）
- Step 4 prompt 增加"结构分析与降维"必写章节: ≥8 维协同问题必须论证
  判据简化（如底面圆周充分性证明）/ 几何降维（预测拦截点）/ 分层分解
  （先分配后优化）三者至少其一，否则 critic 打回。
- method_library/ 补三张动作卡（来源标注为往年范文方法论）。

---

## P2-F 验证与重跑

1. **单元回放**: 用 rerun_0706 的现场做回归——59B 假会话 log → A1 判 FAIL;
   stub 存在 → B3 判 FAIL; de-maxiter=3 provenance → B2 判 BUDGET_LIMITED;
   4.53=4.53 → C1 判 FAIL; 07:52 vs 10:18 mtime → C4 判过期。五发五中才算修完。
2. **盲跑 2025A**: 隔离 E1 后、不开 consult、per-step 全默认，重跑一次
   `cumcm_2025_a_blind`。这是修复后的真实能力读数，可与范文对照
   （对照发生在赛后 evaluation/，不在运行时）。
3. **代理稳定性**: unity2 canary 连续失败时告警（Telegram hook 复用
   CONSULT_TELEGRAM 通道），避免整场运行在降级模式下静默完成。

## 实施顺序

| 批次 | 内容 | 改动面 |
|---|---|---|
| 1 | A1 A2 A3（通道） | run_paper.sh + launch_agents.sh，无 prompt 变更 |
| 2 | B1 B2 B3 + C4（真求解证据 + 时效） | verify_solver.py、verify_step_output、step5 prompt |
| 3 | C1 C2 C3（内生质量） | verify_invariants.py、step5/7 prompt |
| 4 | D1–D4（交付门禁） | run_paper.sh step13/16、step9/15 prompt |
| 5 | E1–E3（答案隔离 + 方法论） | 文件搬迁、step4 prompt、method_library |
| 6 | F 回放 + 盲跑 | 无代码 |

注意: 编辑 run_paper.sh 对活跃 runner 安全（snapshot 机制），但 prompt 契约
变更（STEPS.md/modeling_guide.md/prompts/）影响所有新 step，需一次性成批落地
并记 changelog，避免半新半旧契约混跑。

---

## 实施状态（2026-07-07 完成 批次 1–6）

代码/契约改动全部落地，无活跃 runner，`bash -n` + 单测通过。

| 批次 | 状态 | 关键落点 |
|---|---|---|
| 1 A1/A2/A3 | ✅ | `run_paper.sh`：`_worker_output_valid`（假会话检测，同 attempt 内重试→2 次 return 3）；`backend_canary`+`_backend_health_note`（启动 canary，写 `run_state/backend_health.json`，`BACKEND_CANARY=0` 可关）；codex `requires a newer version` 版本漂移检测；dispatch_step 对 step 4/5/9/12 单点配置告警 |
| 2 B1/B2/B3+C4 | ✅ | 新 `scripts/verify_provenance.py`（provenance 对账/预算下限/stub 残留）；`verify_step_output` 新增 `5)` 分支 + `provenance_gate_passed`/`_verification_is_fresh`；step5 prompt 加 provenance/budget 契约 |
| 3 C1/C2/C3 | ✅ | `verify_invariants.py` 加 `gt_strict`/`nonzero` 类型；step5 prompt 不变量样例从 `ge` 改 `gt_strict`+`nonzero`，加 C2 收敛阶梯/C3 松弛上界契约；values.json schema 加 `upper_bound`/`gap_pct` |
| 4 D1/D2/D3/D4 | ✅ | D1/D2 已在既有主循环（verdict 解析 + 二次 reopen→`.gate2_blocked`），本次新增 D3 `step16_hard_acceptance`（BLOCKING 未清零/stub/provenance 三查）+ D4 step9/15 算法元数据一致性 |
| 5 E1/E2/E3 | ✅ | guide 移至 `evaluation/answer_keys/…_2025A.md`（加禁读横幅+P5 勘误）；新建方法论版 `docs/guides/MODELING_CHECKLIST.md`；step4 改指向 + scope_gate 换内生检查 + §8.1 结构降维必写；preamble 加答案隔离红线 |
| 6 F 回放 | ✅ | `scripts/test_rerun0706_regression.sh` 五发五中（8 断言全过）：假会话/stub/玩具预算/P3==P2/过期验证各被对应 guard 击落 |

**未包含（留给后续）**：C2 目前是 advisory WARN（非硬门禁），待 step5 真产出
`convergence.json` 后可升级为硬失败；D2 的既有实现已足够，未改。**下一步是
批次 F 的盲跑**：隔离答案后重跑 `cumcm_2025_a_blind`（`BACKEND_CANARY=1`、不开
consult、per-step 全默认），拿修复后真实能力读数——这一步需要人来发起。

回归验收随时可复跑：`bash scripts/test_rerun0706_regression.sh`。
