# Changelog

本文档记录 Paper Factory (Modeling Factory) 的重要更新。

## [Unreleased]

### 新增

- LaTeX 构建证据升级为三轮 recorder 与 bibliography 双合同：所有 `.fls` 输入分类为已声明项目文件、未声明项目文件、项目符号链接、受控 TeX runtime、禁止外部文件或允许生成物，异常路径不再静默过滤；编译清理继承的 TeX/BibTeX 搜索环境，启用 `-no-shell-escape`/严格 `openin_any`，并要求三轮项目输入身份一致。
- 新增 `bibliography-build-receipt-v1`：编译前清除当前 job 的旧 `.bbl`/控制文件，按 `\\bibliography` 或 `\\addbibresource` 唯一选择 BibTeX/Biber，任何 backend 失败或未解析 citation 均终止；receipt 绑定 backend 版本、首轮 AUX/BCF、`.bib`、项目 `.bst` 与生成 `.bbl`。最终 fingerprint、acceptance 与 evaluator 合同同时绑定该证据。
- 最终发布链现在在 Final Audit 开始、acceptance 构建及 release pointer 切换前验证实际消费的人工 Approval receipt；不可变 release 会复制这些 receipt 并纳入 delivery manifest。决定 receipt 改为 `O_NOFOLLOW` 单次字节读取，并新增 `scripts/decision_receipt_repair.py`，仅当 SQLite 可重建字节与原 SHA-256 完全一致时恢复缺失文件。
- CI 新增固定 TeX Live/BibTeX/Biber 的 `latex` 作业，真实编译、外部读取、符号链接、旧 `.bbl`、Biber 和未解析引用反例不得因工具缺失跳过。提交 ZIP 使用固定时间戳、权限和成员顺序，可从同一 manifest 确定性重建。
- 新增 `LatexCompileContract`、`-recorder`/`.fls` 输入对账和按命令插入位置生成的展开文档流。静态依赖解析与编译统一使用“主文件目录 → 项目工作目录”搜索顺序；缺失、循环、动态依赖或 declared/observed 项目输入不一致均失败关闭。数字、数字链和符号检查共享展开流，因此跨文件章节状态、插入顺序与 use-before-definition 坐标不再丢失。
- 新增 `submission-bundle-manifest-v1`：最终 PDF、活动 LaTeX 源/参考文献、显式允许的模型/结果/图表和声明附件形成唯一成员集合；final input、Judge packet、submission fingerprint、final acceptance receipt、打包器和 release verifier 绑定同一 manifest。打包拒绝符号链接/越界路径，并在写 ZIP 后逐成员复核中央目录、大小与 SHA-256。
- 新增 Human Decision receipt 读取时强制验证：路径、普通文件/符号链接、大小、SHA-256、schema、request/decision/gate/generation 和数据库决定正文必须一致；Approval receipt 缺失或篡改后 Gate 与 Final Audit 失败关闭，Web diagnostics 显示 `DECISION_RECEIPT_MISMATCH`。
- 新增 schema-v8 审计加固合同：Human Decision 拆分为按 gate/request/generation 和 subject/options fingerprint 绑定的不可变请求与结果；每个决定生成 `.factory/decisions/<gate>/<request>/<decision>.json` 内容寻址 receipt，固定 selection/human-review 文件仅作兼容投影。内容冻结拒绝会清除 pending、失效 Stage 9 之后的 checkpoint 并回到 Stage 9，修复完成后才生成绑定新内容的下一代请求；陈旧的开放请求也可原子标记为 superseded 并重新绑定。
- 新增统一递归 LaTeX 依赖图：从权威论文入口解析 `input`、`include`、`subfile`、`bibliography` 与 `addbibresource`，报告循环/缺失依赖并排除未引用草稿；content freeze、dirty 语义分类、提交/final fingerprint、数字/符号/数值链审计、Judge packet、Web artifact browser 与 submission package 共享活动源合同。
- event-v2 同时记录完成主体与迁移结果坐标，并用 aggregate root 绑定 contest policy、project config、决策、dirty、checkpoint 与 Solver side table；dirty cause 和 Stage checkpoint 增加 append-only 历史，投影失败可记录、诊断和恢复。
- Cloud Solver 请求传输精确输入字节与 SHA-256、声明输出和 seeds，保留 queued/submitting/running 状态并执行实际进程取消；Web 上传改为限额分块写入，普通用户项目申请只接受上传目录内的 PDF/Markdown。
- 新增 GitHub Actions `CI` 工作流，分离 core、LaTeX、Web（含前端构建）与 Cloud/数值依赖测试；`main` 分支保护要求 PR、分支最新且所有 required checks 通过，并阻止 force push 与分支删除。
- Solver Job 新增稳定 `idempotency_key`、回执 `request_sha256`、Stage/subtask/revision/attempt 所有权和唯一约束；Cloud provider 接收幂等键并支持按持久 job ID 对账，本地无法证明提交状态时失败关闭而不盲目重提。
- Web 人工 Gate 采用“证据文件原子 rename + fingerprint → SQLite decision/state/event 同事务”顺序；诊断页直接展示 Native 调度坐标、Recovery Status 与 Audit Timeline，并标记已发布但尚未入账的 orphan decision artifact。
- 新增授权 HMML 完整数据集：保留原始 JSON/Markdown 与来源哈希，确定性展开 97 个可引用方法文档；与现有 21 个精编条目组成双登记表，方法召回升级为“层级分支粗选 → 叶方法细排 → 数据/证据复排”。
- 新增 `problem-plan-v1` 问题专属 DAG：Step 0 必须产出无环、路径受限、方法引用已登记的任务图；后续建模/求解按拓扑依赖承接，最终审计与发布指纹绑定该业务真相产物，但固定十 Stage / 十七 Step 调度权威不变。
- Web 新增 `node-output-v1` / `ContentBlock` 结构化输出协议、前端渲染注册表和“任务图”页；建模方向与问题 DAG 统一渲染摘要、方法卡、依赖图、提示和产物链接，模型上下文使用独立的限长白名单投影。
- 新增 `stage_v1` 10-Stage 权威调度：schema-v6 SQLite 原子持久化 Stage/subtask/source-Step 游标、subtask checkpoint、输入基线、dirty flag 和责任 Stage clear receipt；新项目默认启用，旧 `step_v2` native 项目仅通过显式 `scheduler-activate` 切换，并可在停止且无未清 dirty 时显式回滚。
- 新增独立 Step 8.5 reviewer-entry subtask、条件式 Step 13、双域 paper-audit fingerprint 和 final-input manifest 冻结守卫；科学语义变化按 MODEL/MATH/RESULT/VISUAL/PROSE/CITATION/FORMAT dirty flag 重开责任 Stage，未知变化失败关闭，最终快照变化会中止发布而不复用旧 receipt。
- Web 比赛工作区新增完整 P0–P2 控制台：默认八阶段/可下钻 17 Step、最近三步时间风险预测、持久行动中心、三类 gate-aware 人工决策、canonical/Solver/审计证据驾驶舱，以及绑定 Final Audit 和原子 current release 的交付就绪清单与 PDF/ZIP 下载；Legacy 无比赛时钟时明确显示未配置，所有新接口继续执行项目 ACL。
- 新增 `contest_core_v1`：新建 Native 项目在 schema-v6 SQLite 中持久化默认 74 小时或显式官方 deadline、T−6h content freeze、T−2h delivery freeze 和六小时终端交付保留；共享 deadline 约束 Step、模型/命令、恢复、Final Audit、打包和 release pointer 切换，耗尽后失败关闭。内部 Step 0–16 映射为八个比赛阶段，Step 3 与交付前内容冻结成为默认强制的 SQLite 权威人工节点，冻结后回退需额外人工 override；Web 展示八阶段与冻结/提交倒计时。Solver/Final Audit receipts 与 snapshot hashes 明确保留为不可变机器证据，而 checkpoint、选择 Markdown 和 Web 状态仅为可重建投影。
- 新增不可变原子 release：每个最终快照发布到 `papers/releases/<base>/<snapshot>/`，包含审计 PDF、submission ZIP、manifest 和审计 receipts；全部校验完成后只原子替换 `papers/<base>/current.json`，失败保留旧 current，顶层 PDF/ZIP 仅为兼容副本。
- Web 管理端与 CLI 新增交付 override 签发、查看和撤销；权威记录持久化在 `web/auth.db`，并区分 `continue_after_gate2` 与绑定精确 SHA-256 的 `deliver_snapshot`。
- 新增 `web/backend_service_health.sh`，统一 full/backend-only 部署验收：验证 systemd MainPID、ControlGroup、全部 8000 listener 所有权及稳定窗口内 `NRestarts`，再接受 HTTP 结果。
- 新增 `quality_contract.json` v4：按最大化/最小化方向硬验有效松弛界、预算阶梯、平台期语义和跨算法族对照工件；新增 canonical 派生物 manifest、生成辅助脚本及临时目录重生成/diff 门禁。
- Solver 新增 content-addressed 两阶段 receipt：submission 绑定 runtime、代码、输入、参数摘要和 seeds，completion 绑定终态及声明输出哈希；seed receipt 明确只证明声明，未证明进程实际消费该 seed；native/Legacy 统一通过 `--status <jobid> --json` 返回 fail-closed `solver-job-evidence-v2`。
- 新增 R0a exact-runtime 硬门能力校准：数学/执行角色必须同时覆盖 oracle-backed hard defect 与 neutral transform，并将每个 held-out packet 的 capability observation 与 K>=5 重复稳定性、evaluator/packet/condition hash 逐项绑定；报告失败关闭且不自动放权。
- 新增 R0b pairwise selector 可靠性合同：冻结 dev/holdout family、exact evaluator/packet identity、AB/BA 与重复观测，使用 dev-only TIE 带和 Wilson 界分别报告 proxy/human readiness；未取得独立人工 holdout 前不允许自然稿择优。
- 新增 R3 shadow portfolio 编排器：候选须先通过 R0a 与 R1/R2-min，绑定不可变求解/结果/PDF证据并遵守预算政策；报告 selector 覆盖、TIE、主线分歧、独立 adjudication/regret 与候选数 K，但始终不自动改变主线。
- 新增 selector cutover authorization 校验器：人工 receipt 必须 hash 绑定 ready 的 R0a/R0b/R3 报告，并限定 evaluator、workflow step、项目/题型、最大 K、预算、packet builder、TIE 带、canary 与有效期；assessment 与实际路由事件保持分离。
- Web 新增 Solver Jobs API 与项目工作区面板：按项目 ACL 展示本地/云端作业状态、耗时、输入输出引用及 `solver-job-evidence-v2` 两阶段 receipt 完整性。
- 新增独立 `factory_core.audit` 子系统与 `factory audit` CLI：Step 4、5/6、10 分别运行 `model`、`results`、`paper` 确定性审计并将失败同步到 issue ledger；最终 `final` 审计按内容指纹记录在项目 `.factory/audits/<snapshot>/`。四类审计均可脱离交付运行并复用同一输入与 checker 契约的 PASS，只有 `final` profile 可以授权交付。
- Web 新增独立的完成论文展示 ACL：管理员可分别配置默认未登录访客和具体注册用户的只读论文集合；注册用户继承公共集合，展示授权不授予项目控制、日志或内部文件权限。
- 新增 `factory_core/` Python 编排核心：项目内 SQLite 快照、追加式事件、乐观 revision、注册式 Step/执行后端、重试与验证驱动恢复。
- 新增旧建模项目的两阶段显式迁移和可审计 rollback 命令；活动进程、状态冲突和已退役社会科学项目会拒绝导入。
- 新增原生 Step 0-16 lifecycle/catalog、模型 backend registry、统一 `FactoryService`、SQLite solver policy/job 记录，以及本地与可替换 Cloud Run solver adapter。
- 新增 `apps/`、`benchmarks/`、`legacy/` 和仓库边界/兼容移除文档；运行数据继续保留原位置并可通过 `FACTORY` 指定数据根。
- 新增根 `pyproject.toml`/`uv.lock`、hash-locked Web/Cloud requirements export，并沿用前端 `package-lock.json`。
- Cloud Solver 新增共享 Cloud Run IAM ID Token 获取模块、Python-only 能力清单、镜像构建冒烟测试、Cloud Build 配置预检和按 digest 回滚脚本。
- Cloud Solver 新增恶意路径、环境覆盖、请求/输出上限、低权限 UID 和不可变部署回归测试。
- Web 项目状态增加稳定的题目身份、题目标题、存储域与归档标记；前端按题目内容标识聚合同题多次运行，并可展开历史运行。
- 新增 `AGENTS.md`，作为 Codex 和通用 coding agent 的精简仓库入口。
- 新增聚焦的仓库卫生检查，保护现役文档不再出现默认凭据、旧内存用户库说明或 secret 值展示指令。

### 变更

- Web 求解任务面板将任务状态、两阶段凭证限制/错误码、输入输出字段和标准日志入口统一为中文展示；API 原始值保持不变并保留在诊断提示中。
- Final Audit 统一为最终编译、完整 Step-10 paper/provenance suite、视觉/页数门禁、packet/fingerprint、enforce-mode 三角色 Judge、判决前后快照复核、judgment receipt 与 final acceptance receipt；PASS 复用也必须验证双 receipt 和当前快照。
- Native 与 Legacy Step 16 统一消费同一个 Final Audit 和原子 release publisher；项目清理提前到最终快照构建前，`2026-08-09.atomic_release_v7` 成为当前交付合同。
- 新项目 Step 4 使用 quality-contract v4；Step 5 必须显式声明 solver inputs/outputs/seeds，并由任务内 `FACTORY_SOLVER_JOB_ID` 写 provenance。Step 10 paper audit 新增确定性派生物硬门，旧 v1–v3 合同继续按原边界审计而不被静默升级。
- Shadow cutover manifest 升级为 v2，只有 hash 绑定且 `hard_gate_ready=true` 的 R0a 报告才可能产生 cutover 建议；v1 继续输出诊断但永久 `cutover_ready=false`，所有放权仍需人工批准。
- Step 13 缩为数学单角色预审，`PRECHECK_PASS` 只允许继续摘要与润色；完整数学/执行/论文三角色 Gate 2 仅在 Step 15 后的 `final` 审计执行。Step 15 明确为 `CONTENT_READY` 边界；Step 16 改为独立审计与交付之间的兼容适配器，只消费 `PASS` 或显式 `OVERRIDDEN` 审计结果，复制 PDF、submission 打包和清理不再属于审计职责。
- 移除仓库内 `superpowers-*` agent Skill、配套 workflow 和强制执行规则；
  历史 `docs/superpowers/` 与 `artifacts/superpowers/` 记录继续保留。
- `run_paper.sh` 降级为兼容启动器；新项目默认 `native_v2` 并原生运行 Step 0-16，冻结 Bash 只供未迁移或显式回滚项目使用。
- CLI、Web 状态和项目控制对已迁移项目统一读写 `.factory/state.db`；checkpoint、heartbeat、marker 和 diagnostics 成为兼容投影。
- Web 项目创建/控制直接调用 `FactoryService`，后台执行统一由 Python worker launcher 启动；根 shell 命令保留为兼容入口。
- Web 云策略更新携带 project revision；`.env.cloud` 对 engine 项目降级为投影，不能覆盖 SQLite 或全局 quarantine。
- Web/Cloud 构建改用锁文件，backend/frontend 运行时启动脚本不再创建环境或安装依赖。
- `agy` 模型 SDK 作为 `models` extra 与生产 Web 环境一起锁定；Cloud Solver 依赖集保持隔离。
- 生产 systemd unit 改用仓库根工作目录、根 `.venv` 和稳定 ASGI 入口；部署预检会拒绝旧 unit。
- 前端构建升级到 Vite 8 / Vue plugin 6，并更新 Axios；全新 `npm ci` 审计不再报告已知依赖漏洞。
- Web Dashboard 更新深浅主题、工作区导航与状态视觉层级；选择、咨询、诊断和 Solver Jobs 仍保持独立入口。
- 社会科学执行路径正式退役，历史 prompt、Stata 脚本和项目产物继续保留但不再承诺恢复运行。
- Cloud Solver 鉴权统一为私有 Cloud Run IAM；CLI、监控和 Web 使用同一 ID Token 策略及无密钥专用 Invoker impersonation。
- 云端能力收敛为经过镜像冒烟验证的 Python；API、Web 和 Shell 路由从同一能力清单读取，未安装运行时在提交阶段拒绝。
- Cloud Build 改用 `${BUILD_ID}` 不可变镜像部署并记录 revision/commit/image；`latest` 不再用于生产部署。
- Web 现役文档对齐 SQLite 用户库、bcrypt、注册/管理员审批、项目申请与 ACL 权限模型。
- `web/backend/main.py` 明确为 FastAPI 主入口；`web/backend/app.py` 仅作为兼容启动器。
- 生产敏感值以 GCP Secret Manager 为权威来源；文档和诊断只显示元数据、绑定状态与权限状态。
- Web 部署构建改由服务用户执行，避免 root-owned `dist/` 阻止普通用户后续构建。
- 重复部署、测试和上传报告标记为历史快照，并指向当前 runbook。

### 修复

- 修复 submission ZIP 递归纳入未冻结 `paper/draft.tex`、LaTeX 子文件按错误目录优先级解析、模块化论文数字链丢失父章节状态，以及决定 receipt 删除/篡改后仍可通过 Approval Gate 的四项审计阻断问题；内容冻结拒绝现在记录规范 `WORK_REOPENED` recovery 事件与失效 checkpoint 清单。
- Web 相对时间格式化同时接受 Unix 秒级/毫秒级时间戳、数字字符串和日期字符串，避免 Solver Jobs 返回整数 `requested_at` 时触发渲染异常并使“求解任务”页整体空白；窄屏任务行改用三行自适应布局，完整保留状态、耗时、时间与 receipt 入口。
- Web 八阶段流程下钻区提高标题对比度，并为已完成、运行中和待处理步骤使用与状态底色匹配的前景色，避免步骤文字与实心状态背景同色而不可读。
- Web 审计事项解析在找到首张 issue 表后会于表尾停止，避免把后续增量审计表的 `Severity` 列误读为 `Status`，从而在已完成 Final Audit 的项目行动中心虚报未解决事项。
- Final Audit 的 execution packet 不再纳入 `step_*`、`native_judge_*` 或 `native_receipt_*` 审计运行日志，避免 Judge 及 receipt 构建器写入自身日志并使刚通过的快照立即失效；solver 与模型执行日志仍参与证据指纹。
- Final PDF 视觉门禁不再把 1–2 个字符的公式上下标按不可读正文阻断，而是保留为小字 warning；连续文本和数值低于 4.5pt 仍为 blocking。
- Final Audit 的 Judge grounding 基础设施重试现在会把失败的 `ref_id`、错误原因、声明的 `chunk_id` 以及从不可变 packet 中提取的逐字候选原文反馈给对应角色，并在有界轮次内只重跑当前仍为 indeterminate 的角色，避免使用相同提示盲重试或在引用错误已收敛时过早永久失败；严格逐字匹配和三值判定保持不变。
- `solver_submit.sh --args` 生成提交 receipt 时使用 `--argv=<value>` 传递作业参数，避免 `--only` 等短横线开头的 solver 参数被误解析为 receipt 工具自身选项而在启动前失败。
- 显式指定的 `*.verification.latest.txt` 报告在每次运行前清除旧内容，避免历史项目路径或 verdict 被追加到当前 Final Audit packet，造成跨项目证据污染和误判。
- Dev 测试依赖显式锁定 `httpx2`，避免 Starlette 1.3 `TestClient` 回退到已弃用的 `httpx` 兼容层后挂起；超大请求测试改为直接驱动 ASGI middleware，并确认 413 在 JSON 解析前返回。
- 原生审计将科学判退与评委基础设施失败分流：Step 13 仅处理真实 math FAIL，
  最终审计再处理 math/execution FAIL；`INDETERMINATE_REVIEW`、格式/grounding/路由故障只重试当前角色，
  耗尽后明确停止为 `PERMANENT_JUDGE_INFRASTRUCTURE`。只有 packet 证明上游文件确实
  缺失时，才回到该文件最早责任步骤。
- 原生失败与重试事件保留执行器和验证器的结构化 metadata；Step 10 逐项报告
  `failed_check`、report 和 returncode，模型退出 0 但产物缺失统一标记为
  `TRANSIENT_ARTIFACT_MISSING`，不再以 `UNKNOWN` 重跑。
- 模型调度器会隔离已确认不支持/不可用的候选并继续健康 fallback；API judge 输出和
  rendered prompt 路径统一传项目相对路径，避免同一不支持模型与绝对路径错误反复调用。
- 当前 canonical 汇总强制绑定 `chosen_method.md`、逐问题 source 文件和 solver
  provenance；`quality_contract` v3 要求 hard claim 声明数学域，连续时间 hard claim
  必须提供独立事件定位、认证误差界或双实现证据，复用同一采样数组不能 hard PASS。
- 原生隔离评委提示明确覆盖通用 agent 启动读取，禁止读取 guide、human review、memory 和 Git 状态，并要求保留 `judge_packets/<role>/` 角色目录，避免把存在的 packet 误报为缺失。
- 评委证据包改为硬性文件优先；数学包优先纳入每个模型的 `02_model`/`03_solve` 入口，执行包使用 360 KB 上下文预算，确保问题结果、求解日志和验证报告不会被大型附录挤出；声明式 claim 路径必须与实际产物一致。
- 原生隔离评委不再让 Codex 最终回复覆盖 `judge_outputs/*.md` 协议文件；
  Step 13 预审明确忽略流程要求保留的摘要占位符，而 Step 16 最终复审仍将其视为阻断缺陷。
- 原生 Codex backend 现在与兼容 runner 一致，在未显式指定模型时继承
  `CODEX_MODEL`，避免配置模型失败后的内置 Codex 重试静默切换模型。
- 项目级 `gate2_delivery_override.json` 不再具有授权能力，只能作为请求或历史痕迹；
  管理员数据库授权保留真实 verdict/错误证据且不生成虚假 PASS receipt，精确快照交付
  授权在生成最终验收凭据后被消费。
- Step 14 摘要提示不再硬编码“Gate 2 已 PASS”；override 交付必须读取并保留真实
  verdict 与未解决问题，避免后续 agent 把治理旁路误述为质量通过。
- Worker lease 现在在 SQLite transition 内同时核对 PID 与 lease；连续 Step
  执行期间保持 `RUNNING`，任何存活 Worker 都会阻止重复 start，失去 lease
  的旧 Worker 以 `RunnerLeaseLost` 退出且不能提交后续事件。
- 中断恢复不再无条件覆盖人工等待或永久失败状态；recovery reopen 与正常
  reopen 统一写 `STEP_REOPENED` 并共享同一配额。
- migration rollback 同时切换 `control_mode` 和 `runtime_generation`，CLI、Web
  与 `FactoryService` 在回滚后统一使用 Legacy adapter。
- Step 2 候选流不足现在进入标准 `RETRY_SCHEDULED`/`STEP_FAILED` 预算，不再
  从 prepare 抛出非法转换并遗留 Worker 元数据。
- Solver job 使用独立 `job_revision` 完成后端确认；工作流控制事件不再造成
  已启动任务的 external ID 丢失。CLI Worker 与 Web 默认使用同一个
  `build_solver_backends()` 注册表，Cloud Run adapter 仍受全局 quarantine。
- Cloud Solver client 通过权限受限的临时文件组装和提交 JSON，请求正文与大型 working file 不再进入进程参数，避免触发 `ARG_MAX`。
- Web 普通恢复、Step 3 选择和人工咨询回答现在统一调用
  `FactoryService.resume_and_start`，成功后提交 `WORKER_LAUNCHED` 并实际
  启动 worker；此前 Web 只把状态切到 `ready`。
- Web 人机 gate 请求携带 project revision；stale 请求在写决策和启动
  worker 前返回冲突。终止、完成和归档中的项目不能被重新启动。

### 安全

- 前端构建链将传递依赖 `nanoid` 从 3.3.16 锁定升级到 3.3.18，修复自定义生成器在零长度输入下可能无限循环的 `GHSA-2v37-7h3g-55p8`，并恢复 `npm audit` 零已知漏洞。
- Cloud Solver P0 执行层增加严格任务/路径校验、12 MiB 请求上限、输入只读/输出独立、环境允许列表、隔离启动的资源限制包装器、UID/GID 10001 降权及 CPU/内存/磁盘近似量、文件描述符、子进程、输出文件/目录和日志硬限制。
- Cloud Solver 仍保持全局 quarantine：同实例任意代码访问 metadata 和运行服务账号的风险需要独立 Cloud Run Job 或等价 sandbox 才能解除。
- Cloud Run Solver 进入 P-1 安全隔离：移除匿名 Invoker并默认关闭脚本执行；P0 随后将临时双重认证收敛为单一 Cloud Run IAM。
- Cloud Solver 监控改用 ID Token，并将 401/403 与普通服务故障分开处理，认证配置错误不再静默触发普通本地回退。
- `solver_submit.sh`、手动路由和 Web 控制面默认拒绝启用云端执行；本地求解器保持为唯一受支持路径，直到完整 P0 输入隔离验收完成。
- `solver-runner` 的对象管理权限从项目级收缩到专用 Solver Bucket。
- 移除现役及历史 Web 文档中的可用/弱默认登录凭据示例。
- Secret Manager 迁移备份强制使用私有权限，验证流程不再输出 secret 全值或片段。

## [2026-06-24] - 优秀论文可视化与写作框架系统性改进

### 新增

#### 优秀论文基准文档
- **`docs/guides/EXCELLENT_PAPER_VISUALIZATION_BENCHMARK.md`** - 优秀论文可视化基准
  - 四类叙事角色定义：`explain_model` / `report_result` / `validate_result` / `show_limitation`
  - 六条选图规则：视觉锚点、解释图、可信度图、图表分工、路径题三件套、空间分布多宫格
  - 负面模式清单：不画工程流程、不让粗网格抢主图、不为凑数画图
  
- **`docs/guides/EXCELLENT_PAPER_WRITING_BENCHMARK.md`** - 优秀论文写作基准
  - 五条核心规则：
    1. 摘要采用"开头总述 + 逐问交付"（对齐 2024A A242/A163、2025A A196）
    2. 问题分析写成阅卷索引（难点 → 对象/变量 → 方法 → 输出）
    3. 模型求解先报最终采信口径，诊断和未采信分支后置
    4. 验证支撑可信度，不制造不确定感
    5. 删除内部工程痕迹（m1/m2/results/*.json/RELAXED/fallback/workflow）

#### 可视化架构改进
- **Step 8 (visualization)**: 
  - 引入强制叙事角色分类机制
  - `visualization_log.md` 表格新增"叙事角色"和"依据来源"列
  - 增加"每个子问题至少一个视觉锚点"规则
  - 扩充禁止事项清单：工程流程图、粗网格抢主图、为凑数画图

#### 论文写作框架重构
- **Step 9 (paper_draft)**:
  - 摘要结构从"四段法"改为"开头总述 + 逐问交付"
  - 问题分析要求按"难点 → 对象/变量 → 方法 → 输出"写阅卷索引
  - 模型求解章节先报最终采信口径，诊断后置
  - 图表按叙事角色摆放（explain_model → 问题分析，report_result → 模型求解等）

- **Step 11 (constructive_review)**:
  - 新增"图表质量评估"章节，按优秀论文可视化基准检查
  - 写作评估扩充：摘要结构、问题分析索引、结果口径、工程痕迹

- **Step 12 (revision)**:
  - 优先对齐优秀论文基准：逐问交付、采信口径先行、验证支撑可信度、删除工程痕迹
  - 增加"改文字不改底层"红线说明

- **Step 13 (gate2_judge)**:
  - 新增"优秀论文写作基准检查"五项
  - 新增"优秀论文可视化基准检查"五项
  - 摘要素材提示改为"开头总述 + 逐问交付"结构

- **Step 14 (abstract)**:
  - 模板从"四段散文"改为"总述 + 逐问段落 + 可选亮点收束"
  - 每问段必须按"模型/算法 → 关键结果 → 验证或附件"写

- **Step 15 (polish)**:
  - 新增"内部工程痕迹"检查章节
  - 新增"风险措辞重写"章节（"脆弱/翻转" → "验证/收敛/稳定性"）

### 变更

#### modeling_guide.md
- **§LaTeX Document Requirements**: 摘要描述从"四段法"改为"功能导向 + 分问优先逐问交付"
- **新增 §Figure Selection**: 定义四类叙事角色，明确视觉锚点、解释图、可信度图规则
- **§Color Palette**: 从原 Paper Factory 商业配色改为学术配色
  - Deep blue `#2E5C8A` / Brick red `#C04D4D` / Forest green `#4D9D5B` / Amber `#D49B3E` / Royal purple `#6B4D9A`

### 参考材料

本次改进基于优秀论文深度分析（数据由 `scripts/dxs_*.py` 下载至本地 `external/`，分析文稿见 `docs/reference/`）：
- **2024A**: A163, A242, A016, A053（板凳龙题）
- **2025A**: A196（烟幕弹题）
- **2023A**: A0165（定日镜题）

分析报告见：
- `evaluation/recent_runs_vs_excellent_papers_diagnosis.md` - 最近运行与优秀论文对比诊断
- `docs/reference/2024A_writing_comparison.md` - 2024A 写作对标
- `docs/reference/2023_2025A_writing_commonality.md` - 跨年优秀论文写作共性
- `docs/reference/excellent_paper_visualization_study.md` - 优秀论文可视化方案学习

### 预期效果

- **可视化质量**: 主文图密度提升 30%（8-10 张精准图 vs 12-15 张混杂图）
- **审稿效率**: Step 11 审稿图表调整建议从平均 4.5 条降至 1.5 条
- **评委评分**: Step 13 图表质量评分从 7.2/10 提升至 8.3/10
- **摘要相似度**: 与优秀论文相似度从 65% 提升至 85%+
- **工程痕迹**: 残留从平均 8 处降至 <2 处

---

## [2026-06-23] - GCP 集成与文档清理

### 新增
- GCP Secret Manager 集成
- Cloud Run Solver 服务
- 文档结构化重组

---

## [2026-06-22] - Web Dashboard 前端重构

### 新增
- 逐步选模型界面
- 模型管理功能
- 控制台前端重构

---

## [2026-06-21] - 论文写作环节修复

### 修复

- 修复 systemd 旧会话 listener 可用 HTTP 200 伪装部署成功的问题；部署必须证明端口进程属于正式 unit cgroup，且 MainPID/NRestarts 在稳定窗口内不变。unit 改用 control-group 停止语义、停止超时/SIGKILL 收尾和启动限流。
- 修复 Final Audit 未完整重跑最终论文检查、项目文件可自授权、Legacy 交付仍先覆盖 PDF 再打 ZIP，以及 Native 审计后清理导致内容边界漂移的问题。
- P0/P1 级别错误修复
- 数值验证完整性提升

---

## 文档说明

- **[YYYY-MM-DD]**: 发布日期
- **新增**: 新功能或新文件
- **变更**: 现有功能的修改
- **修复**: Bug 修复
- **移除**: 移除的功能或文件
- **废弃**: 即将移除的功能

更多技术细节请参考各版本的 commit 记录。
