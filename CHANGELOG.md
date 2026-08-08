# Changelog

本文档记录 Paper Factory (Modeling Factory) 的重要更新。

## [Unreleased]

### 新增

- 新增 `quality_contract.json` v4：按最大化/最小化方向硬验有效松弛界、预算阶梯、平台期语义和跨算法族对照工件；新增 canonical 派生物 manifest、生成辅助脚本及临时目录重生成/diff 门禁。
- Solver 新增 content-addressed 两阶段 receipt：submission 绑定 runtime、代码、输入、参数摘要和 seeds，completion 绑定终态及声明输出哈希；native/Legacy 统一通过 `--status <jobid> --json` 返回 fail-closed `solver-job-evidence-v2`。
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
- 项目级 `gate2_delivery_override.json` 现在同时覆盖原生 Step 13/16 的评委判退与
  评委基础设施失败：保留真实 verdict/错误证据且不生成虚假 PASS receipt，流程不再
  回退并继续摘要、润色和交付，最终状态明确归类为 `GATE2_OVERRIDE_DELIVERED`。
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
