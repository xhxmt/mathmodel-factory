# Selector 可靠性与 Portfolio 放权实施计划

> 权威范围：R0a、R1 + R2-min、R0b、R3 以及 selector 人工放权。
> 本文是后续实施和断点续跑的唯一计划入口；各脚本的字段级合同仍以
> [`CAPABILITY_HARNESS.md`](CAPABILITY_HARNESS.md)、
> [`JUDGE_RELIABILITY.md`](JUDGE_RELIABILITY.md)、[`STEPS.md`](../STEPS.md)
> 和代码为准。
>
> 状态快照：2026-08-04，分支 `main`，本地 HEAD `22efcfc`，相对
> `origin/main` 领先 1 个提交。开始后续工作前必须重新核对 Git、测试和运行产物，不能把本快照
> 当作持续有效的运行证明。

## 1. 目标和不可越过的边界

这组工作的目标是降低两类不同风险：先阻止无效方案进入比较，再在有效方案之间减少选错。
硬门、质量 selector 和最终 Gate 2 的职责不可互相替代。

1. **先验有效性优先**：数学或执行硬门没有通过的候选不能进入质量排序。
2. **exact runtime 身份绑定**：模型、backend、prompt、schema、packet 任一字节变化，旧校准
   不再证明新运行时。
3. **尺未验证，不择优**：R0b 未在独立 holdout 上通过前，selector 只能产生诊断，不能改变
   主线。
4. **分不清就报 TIE**：差异落在校准噪声带内时，不得用 raw score、`max(K)` 或多数偏好强行
   决胜。
5. **代理证据不冒充人类真值**：确定性扰动只支持其声明范围内的 proxy 结论；自然生成稿之间的
   质量择优要取得独立人工 holdout 证据。
6. **放权必须人工批准**：任何 readiness 字段都只是批准输入，不会自动改路由或交付状态。
7. **Gate 2 只是否决门**：独立最终评委可以阻止错误交付，但其 PASS 不能反推 selector 当初
   选对了，也不能校准 selector。

本文不做奖级预测，不恢复旧 `/100` 分数的跨版本可比性，也不以机械重跑随机优化器证明历史
provenance。

## 2. 依赖顺序

```text
R0a：硬门能否检出机器 oracle 缺陷且保持稳定
  +
R1 + R2-min：每个候选的质量合同、求解 receipt、确定性派生物
  |
  v
R0b：只在同题 hard-pass 候选之间校准质量排序和 TIE 带
  |
  v
R3：portfolio 影子运行，记录“若放权会选谁”，不改变主线
  |
  v
人工批准：限定 runtime、阶段、项目范围和有效期的 cutover receipt
  |
  v
独立 Gate 2：最终否决；不参与 selector 训练或正确性证明
```

R0a 与 R1 + R2-min 可以并行积累证据，但 R0b 的每个候选必须先满足两者对应的准入条件；R3
必须等待 R0b 对相同 evaluator identity 给出有效的 holdout readiness。

## 3. 当前状态矩阵

状态词：`IMPLEMENTED` 仅表示代码存在，`LOCALLY_VERIFIED` 表示本地测试通过，
`EVIDENCE_READY` 表示真实冻结数据满足退出门，`COMMITTED` / `PUSHED` / `AUTHORIZED` 分别独立。

| 阶段 | 代码 / 合同 | 真实校准或项目证据 | Git / 放权状态 | 下一退出条件 |
|---|---|---|---|---|
| R0a | `IMPLEMENTED + LOCALLY_VERIFIED`；2026-08-04 相关 67 项 focused tests 通过，当前仍是未提交工作树改动 | 尚无真实冻结 held-out campaign，也无 `hard_gate_ready=true` 报告 | 未提交、未推送、未放权 | 完成 exact-runtime 数据运行并人工复核 hash 绑定报告 |
| R1 + R2-min | `IMPLEMENTED`，提交 `22efcfc` | 代码合同已具备；每个进入 R0b/R3 的项目仍须单独生成并通过 v4 工件 | 已本地提交；尚未推送 | 目标候选逐一 hard-pass，且 receipt / 派生物验证通过 |
| R0b | `IMPLEMENTED + LOCALLY_VERIFIED`；2026-08-06 新增 `scripts/selector_calibration.py` 与 6 项失败关闭 focused tests | 仍无真实冻结 dev / holdout、无 `comparison_ready_human=true` 质量排序报告 | 未提交、未推送、未放权 | 冻结 exact-runtime dev/holdout manifest，完成真实人工 holdout 并输出有效 TIE 带 |
| R3 | `IMPLEMENTED + LOCALLY_VERIFIED`；2026-08-06 新增独立 `scripts/shadow_portfolio.py` 与 7 项 focused tests，未复用 hard-gate cutover readiness | 仍无真实 shadow portfolio cohort，也无 `portfolio_ready=true` 报告 | 未提交、未推送、未放权 | 先取得相同 exact identities 的 R0a/R0b readiness，再运行隔离 cohort 并完成分歧 adjudication |
| 人工放权 | `IMPLEMENTED + LOCALLY_VERIFIED`；2026-08-06 新增 `selector-cutover-authorization-v1` 校验器与 5 项 focused tests，只验证、不创建批准或改路由 | 无人工签发 receipt，无有效 assessment | `AUTHORIZED=false` | R0a/R0b/R3 evidence-ready 后由人工签发限 scope/有效期/canary receipt，并另记路由事件 |
| 独立 Gate 2 | 最终否决边界已存在 | 只在具体最终 PDF/packet 上产生证据 | 不授予 selector 权限 | 保持 evaluator 隔离并绑定最终提交字节 |

当前工作树还包含 Web、agent rules 和其他用户改动。后续提交必须按路径精确暂存，不能用全量
`git add` 混入无关变更。

## 4. R0a：收口 exact-runtime 硬门能力校准

### 4.1 要回答的问题

只回答：固定的数学/执行评委运行时，在具有机器 oracle 的真实 judge packet 模态上，能否稳定
检出声明的 hard defect，并对 neutral transform 保持 PASS。它不回答候选之间谁更好。

### 4.2 剩余工作

1. **冻结运行时身份**
   - 固定实际 `model + backend + prompt_sha256 + schema_sha256`；
   - 数学与执行 prompt 必须来自当前运行时使用的文件；schema 快照必须与当前严格解析器一致，
     不能用仅为通过 hash 检查而手写的占位文件；
   - prepared packet、原始 judge 输出、grounding receipt 和汇总报告全程按 SHA-256 绑定。
2. **建立 held-out oracle 集**
   - 满足 manifest 的 `minimum_test_cases`，同时覆盖 math / execution；
   - 每个角色都包含 `hard_defect` 与 `neutral_transform`；
   - 按声明的 `project_id`、`problem_id`、`mutation_family` 轴检查开发集与测试集不重叠；
   - mutation 前置/后置 oracle 必须先由 `capability_harness.py prepare` 验证。
3. **采集真实观测**
   - 所有 case 走与目标门禁相同的调用入口和解析逻辑，不手填裁决；
   - 每个 held-out case 至少 K=5 次完整运行；
   - grounding receipt 必须把 finding 引用绑定到 packet 文件、chunk、精确 quote 和行号；
   - 缺失、格式错误或身份不一致保留在分母中并失败关闭。
4. **生成并审阅报告**
   - 先生成 `capability_report.json`，再生成每个 case 的 reliability report，最后运行
     `hard_gate_calibration.py`；
   - 核查 sensitivity、specificity、precision、grounding、neutral flip、position bias、
     indeterminate 和 false reopen 的 Wilson 界；
   - 保存 manifest、原始输出和最终 `hard_gate_calibration.json`，记录准确 runtime identity。

### 4.3 退出门

- 输入合同和 holdout audit 全部有效；
- math 与 execution 都达到预注册阈值，报告 `hard_gate_ready=true`；
- 每个 held-out case 的 capability decision 与 K>=5 稳定聚合一致；
- 人工复核 hash、样本覆盖和失败案例；
- 报告仍为 `automatic_switch_performed=false`、`operator_authorization_required=true`。

`hard_gate_ready=true` 只在报告声明的 mutation scope 和 exact runtime identity 内有效。任何身份
变化都回到本阶段重新校准。

### 4.4 现有命令

```bash
python3 -m pytest -q \
  tests/test_capability_harness.py \
  tests/test_judge_reliability.py \
  tests/test_hard_gate_calibration.py \
  tests/test_shadow_cutover.py

python3 scripts/capability_harness.py prepare evaluation/capability_manifest.json \
  --output-dir evaluation/capability_runs/<run-id>
python3 scripts/capability_harness.py report \
  evaluation/capability_runs/<run-id>/prepared_manifest.json \
  --json-output evaluation/capability_runs/<run-id>/capability_report.json
python3 scripts/hard_gate_calibration.py evaluation/r0a_manifest.json \
  --prepared-manifest evaluation/capability_runs/<run-id>/prepared_manifest.json \
  --capability-report evaluation/capability_runs/<run-id>/capability_report.json \
  --json-output evaluation/capability_runs/<run-id>/hard_gate_calibration.json \
  --require-ready
```

## 5. R1 + R2-min：已实现合同的项目化验收

### 5.1 已实现范围

提交 `22efcfc` 已加入：

- `quality_contract.json` v4：方向感知 bound、预算 ladder、平台期语义和跨算法族对照；
- 最大化 upper bound / 最小化 lower bound 的方向与可行值自洽检查；
- `solver-job-evidence-v2` 两阶段 content-addressed submission / completion receipt；
- `canonical-derived-artifacts-v1`：由 canonical results 隔离重生成并 diff 表格、headline、xlsx；
- audit profile、prompt 和工作流对上述工件的接入。

### 5.2 后续不是“重写合同”，而是给候选补真实证据

每个准备进入 R0b 或 R3 的候选项目必须：

1. 使用 v4 `quality_contract.json` 声明本题 hard claim、优化方向、bound、ladder、plateau、
   cross-check 和 derived manifest；
2. 所有非平凡求解通过 `solver_submit.sh`，最终 evidence 为
   `solver-job-evidence-v2` 且 `receipt_ready=true`；
3. canonical 派生物可以在临时目录中重生成并按声明的比较模式一致；
4. 相关 `model` / `results` / `paper` audit profile hard-pass；
5. 保存失败结果，禁止把缺证据、无效 bound 或非确定性差异降为 warning 来获得准入。

建议逐项目执行：

```bash
python3 scripts/verify_quality_contract.py <project-dir> \
  --json-out <project-dir>/quality_contract_verification.latest.json
python3 scripts/verify_derived_artifacts.py <project-dir> \
  --json-out <project-dir>/derived_artifacts_verification.latest.json
./solver_submit.sh --status <jobid> --json
python3 -m factory_core.cli audit <project-dir> --profile results --checkpoint-step 5
python3 -m factory_core.cli audit <project-dir> --profile paper
```

上述项目化验收是候选准入条件，不改变 R1 + R2-min 代码实现已完成的状态。推送
`22efcfc` 属于独立发布授权，本计划不把“本地提交”写成“远端已集成”。

## 6. R0b：校准 hard-pass 稿之间的同题质量排序

### 6.1 实施产物

2026-08-06 已新增：

- `scripts/selector_calibration.py`：校验数据、运行身份和 pairwise 观测，计算噪声带与 readiness；
- `tests/test_selector_calibration.py`：holdout 泄漏、身份漂移、位置翻转、TIE 和失败关闭测试；
- `evaluation/SELECTOR_RELIABILITY.md`：manifest / report 字段级合同；
- 冻结 manifest 与不可变 run 目录，最终输出 `selector_reliability.json`。

不要把现有 `comparison_ready_proxy` 直接改名成 selector readiness。R0b 需要单独声明用途、候选
准入和 holdout 证据。

### 6.2 数据合同

每个 pair 必须满足：

- 两稿 `problem_identity` 相同，且 PDF/packet 字节固定；
- 两稿都绑定当前 R0a hard-gate identity，并具有 R1 + R2-min hard-pass receipt；
- 标签来源、标签时间和 adjudication 方法显式记录；selector 输入中不得出现标签、学校、奖级或
  可反推身份的路径；
- A/B 与 B/A 平衡，重复运行绑定相同 evaluator 和 packet；
- dev 与 holdout 按 paper/project family 隔离；同一原稿的改写、派生稿和近重复不得跨 split；
- dev manifest 和阈值先冻结，再首次解封 holdout；查看 holdout 后改 prompt、阈值或 schema 必须
  建新版本，旧报告作废。

标签分两级：

- 确定性质量退化或机器 oracle 只产生 `comparison_ready_proxy`，只允许在同类扰动范围诊断；
- 独立、盲化的人工 pairwise/adjudication holdout 才能产生自然候选择优所需的
  `comparison_ready_human`。

### 6.3 指标和 TIE 规则

报告至少包含：已知序对准确率及置信界、AB/BA flip、重复 pairwise flip、格式失败、
indeterminate、按题目和质量轴的覆盖，以及 margin 的经验误差分布。

阈值必须在 dev 阶段写入冻结 manifest，不能在看到 holdout 后调参。报告从 dev 选择候选
`tie_band`，再只用 holdout 验证：

- `abs(pair_margin) <= tie_band`：输出 `TIE`，不得选择；
- `abs(pair_margin) > tie_band`：才允许输出有方向的质量偏好；
- 指定为 must-not-miss 的已知序对一旦反转，readiness 立即失败；
- 未达到预注册准确率、覆盖或稳定性阈值时 `comparison_ready=false`。

`minimum_distinguishable_margin` 和 `tie_band` 必须绑定 evaluator identity、dataset version 和
holdout hash，不能跨版本沿用。

### 6.4 退出门

- 代码与失败关闭测试完成；
- dev / holdout manifest 在运行前冻结且无 family 泄漏；
- 所有候选 hard-pass，标签对 selector 盲化；
- 独立 holdout 达到预注册阈值；
- 输出可复算的 `selector_reliability.json`，明确 proxy / human readiness 和 TIE 带；
- 未授权生产选择，`advisory_only=true`。

## 7. R3：影子 Portfolio

### 7.1 与现有 shadow 工具的区别

`scripts/shadow_cutover.py` 当前比较 legacy/new **硬门决策**，用于判断一条 R0a 评委路由是否
具备人工切换条件。R3 要解决的是多个 hard-pass 候选之间的**方案选择**，需要新的 portfolio
orchestrator 和独立 receipt，不能复用 `cutover_ready` 冒充 selector 正确性。

2026-08-06 已新增 `scripts/shadow_portfolio.py`、对应 schema/测试和
`evaluation/selector_runs/<run-id>/shadow_portfolio_report.json`。

### 7.2 单次影子决策流

1. Step 1–3 方法 streams 与 Step 5 求解候选取得不可变 `candidate_id`；记录输入、代码、预算、
   seed、solver receipt、canonical result 和最终 packet/PDF hash。
2. 按预注册资源政策检查候选是否具有可比较预算，资源差异必须进入报告，不能静默偏爱更高预算稿。
3. 对每个候选先运行 R0a 对应的数学/执行硬门及 R1 + R2-min 验证：
   - `FAIL / INDETERMINATE`：淘汰或送修，不进入质量 selector；
   - `PASS`：进入同题候选集合。
4. hard-pass 候选少于 2 个时，不产生 selector 胜负证据；保留主线并记录原因。
5. 对至少 2 个 hard-pass 候选使用与 R0b 完全匹配的 selector identity：
   - margin 落入 TIE 带：输出 `TIE`，交 R1 硬证据复核或保守保留主线；
   - margin 超出 TIE 带：记录 selector 推荐，但影子期不改变主线。
6. R1 证据可以否决与 bound、cross-check、receipt 冲突的推荐，但“未发现冲突”不等于证明质量
   排序正确。
7. 影子结果与实际主线选择、后续独立盲化 adjudication 和最终 Gate 2 分别记录，禁止用 Gate 2
   PASS 回填成 selector 正确标签。

### 7.3 影子指标

至少报告：

- 候选总数、hard-pass 准入率和按原因淘汰率；
- selector 可判覆盖率、TIE 率和相对主线分歧率；
- 分歧案例的独立 adjudication 胜率 / regret；
- R1 硬证据冲突率、selector 推荐被否决率；
- evaluator 身份漂移、预算不平衡和数据分布漂移；
- 候选数量 K 与错误率的关系，用于观察 winner's curse，而不是只报告最优分。

### 7.4 退出门

- R0a 与 R0b readiness 均对当前 exact identity 有效；
- 所有进入 selector 的候选都绑定 R1 + R2-min hard-pass 工件；
- 影子 cohort 与 R0b dev/holdout 隔离，并达到预注册的项目数、pair 数和覆盖阈值；
- 不存在未解释的 must-not-miss 反转、身份漂移或硬证据冲突；
- 生成完整 shadow report，但生产路由仍未改变；
- 人工审阅后才允许进入下一节的有限放权。

## 8. 人工批准、有限放权与回退

R3 通过后使用已实现的 `selector-cutover-authorization-v1` 校验合同；仍须由人工审阅并签发
receipt。receipt 至少绑定：

- R0a hard-gate report、R0b selector report、R3 shadow report 的 SHA-256；
- model/backend/prompt/schema/packet-builder identity；
- 获准介入的 workflow step、项目/题型范围、最大 K、预算政策和 TIE 规则；
- 批准人、批准时间、理由、有效期和撤销状态；
- `automatic_switch_performed=false` 的评审报告与随后实际路由变更事件，二者分开记录。

首轮只做小范围 canary。出现以下任一情况立即回到 advisory-only：runtime identity 改变、
readiness 过期、shadow/在线漂移超阈值、must-not-miss 反转、R1 硬证据冲突增加、receipt 过期或
人工撤销。

选拔评委与最终 Gate 2 应使用不同模型族；至少也必须使用不同 evaluator fingerprint，且后者
不能访问 selector 推荐、候选分数或落选稿身份。Gate 2 的 FAIL 按当前交付合同阻断；显式 override
仍是治理决定，不是 selector 或论文质量 PASS。

## 9. 断点续跑清单

每次接手先执行：

```bash
git status --short --branch
git worktree list --porcelain
git log -3 --oneline --decorate
find evaluation -maxdepth 3 -type f \
  \( -name '*hard_gate_calibration*.json' -o -name '*selector_reliability*.json' \
     -o -name '*shadow_portfolio*.json' \) -print
```

然后按以下顺序恢复：

- [ ] 精确识别并保留当前脏文件所有者；不要删除 worktree、分支、报告或生成稿。
- [ ] 复跑 R0a focused tests；审阅并单独提交 R0a 代码，不混入 Web/agent 变更。
- [ ] 建立真实 R0a held-out campaign；只有 `hard_gate_ready=true` 才勾选本项。
- [ ] 确认 `22efcfc` 是否已推送/合并；没有就继续标记 local-only。
- [ ] 给 R0b/R3 候选逐一补齐 R1 + R2-min hard-pass 工件。
- [x] 实现 R0b schema、runner、测试和报告合同；2026-08-06 已本地验证，冻结 dev/holdout 与真实报告仍待执行。
- [x] 实现 R3 portfolio orchestrator 与报告合同；2026-08-06 已本地验证，真实纯影子 cohort 仍待 R0a/R0b evidence-ready 后执行。
- [ ] 人工审阅并签发有限范围授权 receipt；未签发前不得改主线。
- [ ] 保持独立 Gate 2 最终否决，并验证其与 selector 信息隔离。
- [ ] 每阶段完成后更新本文件状态矩阵和绝对日期，不另建平行“当前计划”。

每次修改结束运行相关 focused tests、`git diff --check` 和 severity review，并在汇报中分别写明
`IMPLEMENTED / LOCALLY_VERIFIED / EVIDENCE_READY / COMMITTED / PUSHED / AUTHORIZED`，不能用
“完成”覆盖中间缺口。
