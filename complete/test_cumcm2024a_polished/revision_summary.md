# Step 12 Revision Summary — `test_cumcm2024a`

修订轮次: **2 (Gate 2 reopen 后)**
输入 issue 源: `review_comments.md` (Step 11) + `judge_evaluation.md` (Step 13 Gate 2)
Gate 2 verdict: **`VERDICT: REOPEN_REVISION_MODEL`**（整体 64.7/100；唯一 BLOCKING = P4 整链调头不可执行）

处理 issue 总数: 本轮 6 条 = STEP12-N1 + judge 任务清单 J1–J5（1 BLOCKING / 3 MAJOR / 1 MINOR）；已修复 5（J1–J4 + N1），deferred 1（J5 的下游 = 摘要本身，仍归 Step 14）。
（round-1 已处理 Step 11 全部 14 条，状态见 `audit_issue_ledger.md`，此处不重复。）

## 修订前快照

- 本轮（round-2）快照: `paper/archive/pre_step12_round2/test_cumcm2024a_paper.tex`
  - 版本: mtime 2026-05-30 11:03:35（对应 round-1 产出的 paper.tex）
  - 行数: 704（修订后 710）
- round-1 快照仍在 `paper/archive/pre_step12/`（供两轮 diff）。

## 根因诊断（本轮核心）

Gate 2 的 BLOCKING 是 round-1 诚实披露的 P4 全队碰撞（STEP12-N1）。本轮**没有**简单放松题给约束去“绕过”碰撞，而是定位到两处实现缺陷：

1. **调头 S 曲线取了退化分支。** “与盘入/盘出螺线相切 + 前后弧半径 2:1 + 弧间相切”约束系统有\emph{两个}几何分支。round-1 的 `construct_uturn_path` 取到退化分支：后段弧仅 $4.6^\circ$、相切点 $P_c$ 贴附于 $r=4.5$ 边界，曲线整体挤在边界附近，使链体在边界处虚假拥挤而互相干涉。正确分支为两弧角度均衡（各 $173.1^\circ$）、$P_c$ 内收至 $r=1.5$ 的非退化 S 形。
2. **把手沿弧长而非欧氏定弦长布点。** `PolylinePath.state` 旧实现把相邻把手按\emph{路径弧长}间隔 $L$ 放置，而刚性板凳要求相邻把手\emph{欧氏直线距离} $=L$。在曲段上二者差异巨大（紧弧处定弦长残差一度达 $83\%$），人为压缩链体、放大/提前触发碰撞。

修复后（`models/m1_spiral/03_solve.py`）：选非退化分支（$R_1=3.005,\ R_2=1.503$，$R_1{:}R_2=2{:}1$，$L_{\text{Uturn}}=13.621$），把手按欧氏定弦长布点（解析“圆-线段”求交，杆长残差 $1.55\times10^{-15}\,\mathrm{m}$）。全队 SAT 实体扫描 `no_collision=true`、$0$ 事件、最小裕度 $0.290\,\mathrm{m}$。

## 逐 issue 修订记录

### J1 / STEP12-N1 [BLOCKING]（judge #1）: P4 全队调头路径不满足实体无碰撞

- **修订位置**: 代码 `03_solve.py::construct_uturn_path`（新增 `_scurve_branch` 选非退化分支）、`PolylinePath.state`/`_back_point`（欧氏定弦长布点）；`paper.tex` §6.4 几何式、“全队实体无碰撞核验”段（原地重写）。
- **修订前**: round-1 报告 $R_2=2.251$、$t\ge16\,\mathrm{s}$ 起 $201$ 帧中 $85$ 帧碰撞、首碰对 $(0,2)$，并把 P4 列为“整链物理可执行性存疑”的局限。
- **修订后**: 非退化分支 $R_2=1.503$、全程 $0$ 碰撞、最小裕度 $g^{\text{P4}}_{\min}=0.289538\,\mathrm{m}$（$t=30\,\mathrm{s}$，板凳对 $(0,16)$）；§6.4 改述为“整链物理可执行”。
- **依据**: `results/problem4/p4_collision_scan.json`（`no_collision:true`，`total_collision_events:0`，runtime 293.9 s，本步骤重跑）。
- **副作用 verification**: 经 `solver_submit.sh` 重跑 `03_solve.py --sub=problem4/problem5`、`08_p4_collision_scan.py`；杆长残差 $1.55\times10^{-15}$；旧 P4/P5 结果备份为 `results/problem{4,5}/values.round1.json` 等。

### J2 [MAJOR]（judge #2）: “能否更短”需在无碰撞可执行口径下重答

- **修订位置**: §6.4 “能否更短”段 + 表~`tab:p4_shorter`（改为三行对照）。
- **修订后**: 明确判定——在“相切 + 半径比 2:1 + 全队无碰撞”口径下，非退化曲线 $13.621\,\mathrm{m}$ 已是\textbf{最短可执行}解，不能更短；它反而\textbf{短于}退化分支 $13.965\,\mathrm{m}$；m2 NLP 的更短解（$2.131\,\mathrm{m}$）塌缩至中心、整链不可通过，不采信。
- **依据**: `results/problem4/values.json::auxiliary_validation`（m2 obj 2.131、max radius 1.022）；新 `uturn_length=13.621230`。
- **副作用 verification**: 无新增求解（复用 P4 重算结果与 m2 对照）。

### J3 [MAJOR]（judge #3）: P5 依赖新 P4 路径，安全速度重算

- **修订位置**: §6.5 两段 + boxed 答案；§7.4 表~`tab:s5` + 段落；图注 `fig:p5_speed`、`fig:sens_p5`。
- **修订前**: $v_0^\star=1.482792\,\mathrm{m/s}$（$\Delta t=0.25$，基于旧路径）。
- **修订后**: 新路径上 $\Delta t=1/0.5/0.25\,\mathrm{s}$ 的最大倍率 $1.385647/1.588882/1.588882$，安全速度 $1.443369/1.258747/1.258747$；推荐 $v_0^\star=1.258747\,\mathrm{m/s}$（$0.5$/$0.25$ 收敛），整数秒上界 $1.443369$。下降幅度由 round-1 的 $-18.4\%$ 改为 $-12.8\%$。新路径后段弧更紧（$R_2$ 由 $2.251\to1.503$），速度放大更大、安全速度更低，物理自洽。
- **依据**: `results/sensitivity/s5_p5_dt_sweep.json`、`results/problem5/values.json`（本步骤重跑）。
- **副作用 verification**: `solver_submit.sh` 跑 `05_sensitivity.py --sweep=s5_p5_dt_sweep`。

### J4 [MAJOR]（judge #4）: 附件、图表与正文同步新 P4/P5 结果

- **修订位置**: `result4.xlsx`（`results/problem4/` + 根目录）；`figures/*`（P4/P5/s4/s5）；`paper.tex` 表~5/6/7、§6.4/§6.5/§7.3/§7.4/§8.2/§9、`concept_subproblem_dag` 与各图注。
- **修订后**: `04_postprocess.py` 由新 NPZ 重生成 `result4.xlsx`（根目录副本 md5 一致 `6ebed44c…`）；`06_figures.py --mode all` 确定性重生成全部图（仅底层数据更新，风格代码未改；旧图存 `figures/pre_step12_round2/`）；正文表 6/7 全部 70+35 单元、§7.3 表 s4、§7.4 表 s5 数字与 results/* 抽样核对一致。s4 重述：$R_{\min}$ 由“主导量”降为 m2 退化下界。
- **依据**: `results/problem4/result4.xlsx`、`results/sensitivity/s4,s5`、`results/problem{4,5}/values.json`。
- **副作用 verification**: `solver_submit.sh` 跑 `04_postprocess.py`、`05_sensitivity.py --sweep=s4_p4_rmin_compare`、`06_figures.py`；新增 `Pc/c1/c2` 透传到 `load_auxiliary_m2` 以修复对照图缺键。

### J5 [MINOR]（judge #5）: 为 Step 14 摘要准备新版素材

- **修订位置**: 本文件 §给 Step 13 (Gate 2 judge) 与 Step 14 摘要的提示。
- **修订后**: 列出 P1–P5 最终口径，明确 P4 现为\emph{可执行}（无碰撞，min 裕度 0.290 m）、P5 速度随路径改变降至 1.259 m/s。
- **副作用 verification**: 无新增求解。

## 未处理 issue (deferred)

- **B1 [BLOCKING] 摘要仍为 `ABSTRACT_PLACEHOLDER`** — 仍按工作流约定 defer 到 **Step 14**（在 Gate 2 之后）。Step 12 禁止填占位符且 `infer_step` 依赖其识别未进 Step 14。judge 也已认定这不是本轮 model reopen 的主因。关键数值口径见下方 §Step 14 提示。
- **图像内容的视觉复核**: 本轮已用确定性脚本 `06_figures.py` 重生成受影响图（P4 几何/快照、P5 放大、s4/s5），使图与正文/附件一致（直接回应 judge #3「总览图改最终口径」、#4「重生成图表」）。这是对“Step 12 不重画图”规则的\emph{有据偏离}：模型已变，留旧图必然造成图-文冲突而再次 FAIL Gate 2；本轮仅\emph{重跑既有 Step-8 绘图管线}（非手绘新图），风格代码未动。若 Step 13/Step 8 要进一步美术润色，可在此基础进行。

## 假设登记簿状态变更 (assumption_ledger.md)

- **A5**: 状态保持 **RELAXED**，标签 **CRITICAL 未动**（按 prompt 红线）。仅做\emph{陈述微调}：$R_{\min}=2.0$ 由“P4 主曲线下界”降格为“辅助 NLP 模型 m2 的退化保护”；主曲线可执行性改由全队实体无碰撞核验（C5）锚定。此变更与 ledger 早先记录的 Step-6 处理义务（“reopen 重算 P4 应优先补实体占用约束、勿把 $R_{\min}=2.0$ 当无条件常量”）一致，已在 §依赖 与 §后续步骤处理义务 标注「Step 12 round-2 已执行」。
- A1–A4、A6 无变更；PROTECTED（A3/A4）与 CRITICAL（A1/A2/A5）标签均未触碰。

## 跨步骤 issue ledger 同步

`audit_issue_ledger.md` 已更新: STEP12-N1 OPEN → **RESOLVED**；新增 STEP13-J1（BLOCKING）/J2/J3/J4（MAJOR）/J5（MINOR）均 **RESOLVED**；表头记录 Gate 2 verdict 与 round-2 根因。

## Sanity check verification

- [x] `compile_paper.sh "$(pwd)" test_cumcm2024a` exit 0（31 页，xelatex，PDF 12:20 生成）
- [x] paper.tex 仍含 `ABSTRACT_PLACEHOLDER` 恰 1 次（Step 14 未到，未填）
- [x] paper.tex 含 `\begin{document}` 与 `\end{document}` 各 1 次
- [x] LaTeX log 无 undefined reference / citation
- [x] 所有 `\includegraphics` 目标在 `figures/` 存在
- [x] 所有 `\cite{}`（13 个 key）在 `references.bib` 存在
- [x] 数字与 results/* 一致（抽查 9 项：R1 3.005418 / R2 1.502709 / L 13.621230 / 碰撞 no_collision+0.289538 / P5 1.258747 / mult 1.588882 / P5 整数 1.443369 / P4 表 head x(-100) 7.775117 / s4 m1_R 1.503，全部 PASS）
- [x] 根目录 `result4.xlsx` 与 `results/problem4/result4.xlsx` md5 一致（`6ebed44c…`）

## 给 Step 13 (Gate 2 judge) 与 Step 14 摘要的提示

本轮针对 Gate 2 的 `REOPEN_REVISION_MODEL` 重点：

- **BLOCKING 已解除**: P4 不再是“不可执行”。根因是 round-1 取了退化 S 曲线分支并用弧长（而非欧氏定弦长）布点；改正后全队 $-100$–$100\,\mathrm{s}$ 逐秒 SAT 扫描\textbf{无碰撞}（$0$ 事件，最小裕度 $0.290\,\mathrm{m}$）。这是真正的模型修复，而非放松题给约束绕过碰撞——仍保持“相切 + 半径比 $2{:}1$ + 调头圆内”。
- **MAJOR 全部已处理**: J2（更短判定）、J3（P5 重算）、J4（附件/图/正文同步）。
- **P4/P5 路径变更已传导**: result4.xlsx、相关图、s4/s5、§6.4/6.5/7.3/7.4/8.2/9 全部同步。
- **P1–P5 最终口径（供 Step 14 摘要直接写入）**:
  - P1: $p=0.55$、$v_0=1$，$0$–$300\,\mathrm{s}$ 全队逐秒；杆长残差 $3.33\times10^{-12}\,\mathrm{m}$。
  - P2: 无碰撞终止 $t^\star=412.473838\,\mathrm{s}$（首碰板凳对 $(0,8)$）。
  - P3: 推荐保守最小螺距 $p^\star\approx0.450\,\mathrm{m}$（61 点加密认证）；$0.429883$ 为 7 点粗网格下界。
  - **P4（新）**: 非退化 S 形调头曲线 $L_{\text{Uturn}}=13.621230\,\mathrm{m}$（$R_1=3.005,\ R_2=1.503$，$2{:}1$，$P_c$ 内收至 $r=1.5$），\textbf{全队无碰撞、物理可执行}（最小裕度 $0.290\,\mathrm{m}$）；保持相切与无碰撞前提下不能更短。
  - **P5（新）**: 推荐龙头最大安全速度 $v_0^\star=1.258747\,\mathrm{m/s}$（$\Delta t=0.5$/$0.25$ 收敛）；整数秒上界 $1.443369\,\mathrm{m/s}$。
- **剩余诚实局限**: P4 无碰撞裕度仅 $0.290\,\mathrm{m}$，对板凳真实厚度/销轴间隙/公差敏感，建议预留额外安全余量（§8.2 第 4 条）。
