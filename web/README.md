# Paper Factory Web Dashboard

本目录提供 Modeling Factory 的 Web 控制面。它负责公开论文展厅、用户与项目审批、比赛阶段与时钟监控、日志/文件查看、三类人工决策、证据汇总和原子交付下载。对 engine 项目，创建、控制与云策略直接调用 `FactoryService`；未迁移项目才进入显式兼容路径。

当前文档分工：

- 本文：功能、权限、开发启动和 API 概览。
- [`QUICKSTART.md`](QUICKSTART.md)：最短本地启动路径。
- [`USAGE_GUIDE.md`](USAGE_GUIDE.md)：面向访客、普通用户和管理员的操作流程。
- [`docs/deployment/DEPLOYMENT.md`](docs/deployment/DEPLOYMENT.md)：唯一现役生产部署与回滚 runbook。

`docs/` 下的其他部署、测试和功能完成报告都是日期化历史快照，不能替代上述现役文档。

## 当前能力

项目概览提供默认关闭的“GPT Pro + Claude Fable 联合建模”开关。新建时选择“仅创建”，随后人工开启并启动项目。候选生成、Pro 回填、Claude 综合和人工选模沿用现有工作流；详见 [联合建模操作说明](../docs/operations/JOINT_MODELING.md)。

正常运行的证据和状态合同见 [Normal-run audit contracts](../docs/operations/NORMAL_RUN_AUDIT_CONTRACT.md)。项目概览分别显示当前执行、工作流错误、证据有效性、科学结论、诊断分数与交付状态；数学预审没有正式分数或交付权。恢复后历史错误仅保留在审计时间线。后台启动等待 worker 初始化确认，初始化失败会留下失败状态。

- 管理员可分别配置未登录访客和具体注册用户可阅读的完成论文；展示 ACL 与项目控制 ACL 独立。
- 用户可注册账号；新账号默认是 `pending`，管理员审批后才能登录。
- 认证和审批状态持久化在 `web/auth.db`，密码使用 bcrypt 哈希。
- 普通用户提交项目申请，管理员审批后创建项目并写入项目 ACL；普通用户只能看到获授权项目，管理员可见全部项目。
- 管理员可直接创建项目，并管理用户、项目申请、完成论文展示权限、Secret Manager 元数据状态和审计日志。
- Dashboard 将题目内容相同的多次运行按 canonical SHA-256 身份聚合为一个“题目归档”。这只是展示层分组，不移动或改名 `ongoing/`、`complete/` 中的目录。
- 进行中的运行可暂停、恢复或终止；完成归档保持只读。
- 项目概览显示 10 个持久调度 Stage，可下钻到 Step 0–16；当前步骤采用后端原生 Stage/subtask/source Step 位置，最终审计执行中不会提前显示完成。比赛时钟仍按 8 个比赛阶段组织。最近三步平均耗时用于预测内容完成时间和 content-freeze slack；Legacy 项目没有比赛 policy 时明确显示“未配置”，不虚构倒计时。
- 人工选模与咨询均进入待办；中断、重试、暂停和失败分别显示状态与对应操作。证据有效性、科学结论及交付许可独立展示，证据变化即使没有 workflow revision 更新也会刷新。此视图尚不提供每一步历次执行的完整数据库时间线。
- “任务图”页展示 Step 0 的 `problem-plan-v1` 问题专属 DAG；固定 Stage/Step 仍是调度权威，DAG 只表达题目内部的数据、参数、求解和验证依赖。
- 建模方向与任务图 API 使用统一 `node-output-v1` / `ContentBlock` 协议，前端按注册的 `render_type` 渲染摘要、方法卡、DAG、提示和产物链接；后端不下发可执行 HTML。
- 顶部行动中心持续聚合 Human Gate、deadline 风险、Solver 失败、未解决审计事项和交付阻塞。
- `step3`、`content_freeze`、`delivery_freeze_override` 三类人工决策可在 Web 中完成，均携带当前 revision、request ID、generation 和 subject/options fingerprint 并写入 append-only SQLite；拒绝审批会保留拒绝结果并打开下一代请求，CLI 路径始终保留。
- 证据驾驶舱汇总 canonical results、PRIMARY/AUXILIARY、Solver jobs/receipts、model/results/paper/final audits 与三角色状态。
- 论文 artifact 分组来自完整活动 LaTeX dependency graph（含嵌套 bibliography）；决定 receipt 的缺失、符号链接、哈希或身份不一致显示为 `DECISION_RECEIPT_MISMATCH`，Approval Gate 不会继续放行。缺失 receipt 只能通过 `scripts/decision_receipt_repair.py <project> <request_id>` 从不可变 SQLite 决定重建，且重建字节必须匹配原 SHA-256；已有损坏证据不会被覆盖。
- 交付就绪中心按红黄绿列出 PDF、canonical results、附件、内容冻结、确定性检查、视觉页数、三角色、最终快照和原子 release；PDF/ZIP 只从已验证的 current release 下载。
- Phase 6 候选提供可选的“验证快照”只读页：它从独立 SQLite 展示与单一 revision 绑定的 snapshot/section 哈希证据，默认不进入前端包且后端默认不访问其数据库。该页不授予 Authority、项目 ACL、交付权或 dispatch 能力。
- Phase 7+8 候选提供默认关闭的 durable local full-shadow API：受控 OS 操作员先生成可信 preflight，普通 subject 再通过 CLI 或项目 ACL 保护的 Web API 提交、显式执行和读取 grounding/PDF-CAS/egress shadow 结果。它没有 Phase 7+8 前端面板、后台 worker、provider、outbox 或真实 dispatch。

## 本地启动

### 前置条件

- Python 3、Node.js `^20.19.0` 或 `>=22.12.0`、npm、`gcloud` CLI。
- 当前 GCP 项目中已配置 `scripts/load_secrets.sh` 使用的 Secret Manager 条目。
- 当前账号有对应 Secret 的访问权限。
- 已在仓库根目录运行 `uv sync --extra web --extra models --locked`，并在 `web/frontend/` 运行 `npm ci`。

生产敏感值以 GCP Secret Manager 为权威来源。不要把 `JWT_SECRET`、`ADMIN_PASSWORD` 或 API key 写入 `.env`、文档、测试输出或命令日志。

### 配置非敏感项

```bash
cd /home/tfisher/paper_factory
cp web/.env.example web/.env
```

编辑 `web/.env`，至少设置 `GCP_PROJECT_ID`。该文件只应包含非敏感配置；部署预检会拒绝其中出现敏感键。

### 启动前后端

```bash
cd /home/tfisher/paper_factory/web
./start_dashboard.sh
```

打开 <http://localhost:5173>。后端健康入口是 <http://127.0.0.1:8000/>。

系统没有默认管理员密码，也不会自动生成 JWT Secret。后端启动时会校验两者；缺失、过短或弱默认值都会阻止启动。管理员账号名为 `admin`，密码来自 Secret Manager 中的当前配置。

## 权限与用户流程

### 访客

访客进入公开论文展厅，只能读取管理员授予“默认未登录用户”的完成项目最终 PDF，不能查看内部项目、日志或控制动作。

### 普通用户

1. 在登录界面注册账号。
2. 等待管理员审批；`pending`、`rejected` 或 `disabled` 用户不能登录。
3. 上传 PDF、Markdown 或压缩包，并提交项目申请。
4. 管理员审批后，项目由 `FactoryService` 创建，申请人获得该项目的 owner ACL。
5. 用户只能查看和控制 owner ACL 授权的项目；另可在只读展厅查看公共论文及管理员单独授予自己的论文。

### 管理员

管理员可以：

- 审批、拒绝、禁用或删除普通用户；`admin` 自身不能被删除。
- 审批或拒绝项目申请，也可直接创建项目。
- 在 `complete/` 中带最终 PDF 的项目范围内，按默认访客或具体注册用户维护展示权限。
- 查看全部项目、Secret Manager 元数据健康状态和最近审计记录。
- 对进行中的项目执行暂停、恢复和终止。

展示权限持久化在 `web/auth.db` 的独立 ACL 中。`SHOWCASE_PROJECTS` 仅在数据库首次升级到该结构时初始化默认访客集合；初始化完成后，包括空集合在内的管理员配置都不会再被环境变量覆盖。注册用户看到访客公共集合与个人授权集合的并集。

## 上传与创建项目

上传端点支持：

- 单文件：`.pdf`、`.md`
- 压缩包：`.zip`、`.tar`、`.tgz`、`.tar.gz`、`.tar.bz2`、`.tar.xz`

默认大小上限为 100 MB，可通过 `MAX_UPLOAD_SIZE` 调整。压缩包会在 `uploads/` 下安全解压并查找题目 PDF/Markdown；目录穿越归档会被拒绝。

管理员创建项目等价于 CLI 命令：

```bash
python3 -m factory_core.cli create <base_name> "/abs/path/to/problem.pdf" [--consult] [--start] [--contest-deadline <epoch-or-ISO8601>]
```

稳定的生产 ASGI 入口是 `apps.web.backend.main:app`；
`web/backend/main.py` 在迁移期保留 FastAPI 实现，`web/backend/app.py` 仅兼容重导出。

普通用户走项目申请/审批流程，不会绕过管理员批准。

## 题目归档

项目列表返回以下展示字段：

- `run_id`：当前运行目录名，与 `base_name` 一致。
- `problem_key`：优先根据项目内题目源文件计算 `sha256:<digest>`；没有可用源文件时退回 `project:<base_name>`。
- `problem_title`：从项目内 Markdown 标题提取，失败时使用项目名。
- `storage_scope`：`ongoing` 或 `complete`。
- `archived`：是否位于 `complete/`。

前端按 `problem_key` 分组并显示每道题的最新运行和历史运行。`ongoing/` 与 `complete/` 是存储位置真相；新项目和已迁移项目的运行状态来自项目内 `.factory/state.db`，响应携带 `revision`。归档 UI 本身不是数据迁移。

## 人工选择：Web 与 CLI 并行

新 `contest_core_v1` 项目会无条件在 Step 3 前等待选择；仅 Legacy 项目继续使用 `selection/config.json` opt-in。可以在 Web 中提交，也可以在仓库根目录运行：

```bash
python3 scripts/selection_gate.py select-step3 ongoing/<base_name> \
  --primary m2 --aux m1 --reason "Prefer the verified primary stream"
```

决策以项目 SQLite 的 request/decision ledger 为权威，JSON/Markdown 仅为界面与 Agent 投影。Step 16
前还会出现 `content_freeze` 人工节点；CLI 可运行：

```bash
python3 scripts/selection_gate.py approve-content-freeze ongoing/<base_name> \
  --reason "Conclusions, abstract and figures reviewed"
```

调试时可加 `--no-resume`。Web 提交选择或咨询回答时携带当前 project
revision 和当前 request identity；过期页面会收到 `409`，不会写入旧决策或启动 worker。成功提交
会解析 gate 并启动统一 Python worker。CLI 路径是现役合同，不能被 Web
替代。

## Phase 6 验证快照（默认关闭）

Phase 6 是候选级 full-shadow 读路径，不是 v1 工作流或控制面的替代品。
后端先完成认证和现有 `web/auth.db` 项目 ACL 校验，之后才检查 Phase 6
开关、解析路径并延迟导入 shadow store。Phase 6 scoped grant 本身不会授予
Web 项目访问权；HTTP API 也不会创建、签发、撤销或评估 grant。

端到端启用必须同时满足：

1. `PHASE6_SNAPSHOT_ENABLED` 为后端认可的 true 值；缺省为 `false`。
2. 前端构建环境中的 `VITE_PHASE6_FULL_SHADOW_ENABLED` 精确等于小写字符串
   `true`；`1`、`TRUE` 和布尔值都不会启用前端。
3. `PHASE6_SNAPSHOT_DB_FILE` 指向已经由受审查 Phase 6 producer/harness
   建立的绝对路径；其父目录必须预先存在且不能经过符号链接。未显式设置时，
   默认是 `<FACTORY_ROOT>/run_state/phase6_snapshot_shadow.db`。

本地后端非敏感配置可写入 gitignored 的 `web/.env`：

```dotenv
PHASE6_SNAPSHOT_ENABLED=true
PHASE6_SNAPSHOT_DB_FILE=/home/tfisher/paper_factory/run_state/phase6_snapshot_shadow.db
```

Vite 不读取父目录的 `web/.env`。本地开发应把下行写入 gitignored 的
`web/frontend/.env.local`，生产构建则写入
`web/frontend/.env.production.local`：

```dotenv
VITE_PHASE6_FULL_SHADOW_ENABLED=true
VITE_PHASE6_SNAPSHOT_DEADLINE_MS=15000
```

`VITE_PHASE6_SNAPSHOT_DEADLINE_MS` 是构建期正整数配置，范围 1–300000ms，
缺省 15000ms；修改后必须重新构建。它是一份总预算，同时覆盖等待响应头、读取/
解析 body 和最多一次 stale retry，不会为 retry 重新计时。deadline 同时触发
AbortController 并与 transport Promise race，因此忽略 signal 的自定义 transport
也不能让页面永久保持 loading。

不要仅打开一侧：后端开、前端关时 V1 页面保持不变，但已授权调用方仍可访问
API；前端开、后端关时标签页会把后端 404 映射为“暂不提供快照”。完整回滚是把
两侧都设为 false（或移除本地前端 flag 文件）、重新构建前端并重启后端。
回滚不会删除独立数据库。

API 只读入口是：

```text
GET /api/projects/{base_name}/phase6-snapshot?expected_revision=<nonnegative integer>
```

成功响应使用 `phase6-project-snapshot-web-v1`，且
`authoritative`、`authority_transferred`、`dispatch_performed` 必须全部为
`false`。`expected_revision` 过期返回 409，并允许前端仅重试一次无 revision
读取；认证/ACL 失败、禁用、PARTIAL/ineligible source、缺失、篡改或不可用状态
都失败关闭。公开错误只返回
有限 code，不包含数据库路径、SQL 或内部异常消息。前端保留 loading、ready、
empty、legacy_unavailable、auth_error、api_error、unknown 七种状态；只有同一
有效坐标的 ready 页面才能显示 sections 或“无待处理事项”。

查询参数也服从 ACL-first：后端在项目 ACL 通过后才把原始
`expected_revision` 解析为非负整数。因此，无项目权限的 active 用户无论省略、
传入负数还是传入非数字，都会得到同一项目拒绝；有权限用户的非法值才得到干净的
参数 4xx。Action ID 去除首尾空白后必须唯一，重复 ID（相同或不同 payload、任意
顺序）使整个 ready 投影失败关闭。仅稳定 allowlist 中的错误有专用提示，其他后端、
传输或本地异常统一显示“项目快照服务暂不可用”，内部路径、SQL、凭据和堆栈不进入
页面。timeout 映射为安全的 unavailable；用户取消、reset、页面离开和新 generation
替换均具有不同的内部原因并结束 loading，旧请求的 deadline、完成或取消不能覆盖
新请求。

`npm run test:phase6` 会执行真实 Vite production 双构建和 Chromium 挂载流程：
默认关闭时 manifest、chunk、路由和网络资源均不得出现 Phase 6；启用时验证 lazy
模块、实际请求、键盘焦点（方向键、Home/End、Tab、Enter/Space）、disabled 状态和
ARIA busy/live/alert。该测试需要先 `npm ci` 并准备与 lockfile 匹配的 Chromium。
Vite 会在构建时把 `virtual:optional-workspace-snapshot` 精确映射到 inert disabled
模块或显式 enabled 模块；基础 `ProjectWorkspace` 只消费通用扩展接口。不要改成
无条件 import 后再靠运行时 `v-if` 隐藏，否则默认构建的 manifest/module graph
和浏览器资源门禁会失败。

Phase 9 的正式 `full_repository` 证据会在 source 与 fresh 环境中按同一顺序运行
全仓 Python pytest、独立 frontend production build、以及这里记录的
`npm run test:phase6`。它要求显式的只读 `node_modules` 与 Chromium runtime，
通过 `PHASE6_CHROMIUM_EXECUTABLE` 绑定浏览器，并由已记录和哈希的 Node 直接
执行解析后的 npm CLI（而不是只做 Node 版本探测）；独立 production build 的
Vite config loader 固定为 `--configLoader runner`，从而绕开默认会尝试在只读
`node_modules` 下物化 `.vite-temp` 的 bundled-config 路径；`outDir` 放在本次
调用的可写 basetemp 中。runner 会把
`package.json` 中精确的
`vite build` 和两文件 `test:phase6` 脚本绑定到源码 inventory，并对 build 的
非空 `index.html`、`assets/` 文件及其 SHA-256 清单做前后复核。缺少依赖/浏览器、
空 build、浏览器 skip 或任一阶段非零都使整个 suite 失败；source/fresh 的 build
清单、浏览器测试节点和结果也必须逐项一致，不能只靠相同通过计数取得证据 PASS。

Phase 6 的完整身份、持久化、grant 生命周期和安全边界见
[`../docs/architecture/PHASE6_PROJECT_SNAPSHOT_UI_SHADOW.md`](../docs/architecture/PHASE6_PROJECT_SNAPSHOT_UI_SHADOW.md)。

## Phase 7+8 durable local sidecar（默认关闭）

Phase 7+8 是候选级同步本地 full-shadow sidecar，不是现役 workflow、交付或
provider 控制面。默认 `PHASE78_ENABLED=false` 时，后端不注册 Phase 7+8
router；CLI、service、Scheduler 和 worker 也在读取 request file、解析路径、
打开 SQLite/CAS、导入重模块或启动线程/进程前返回。该能力没有前端页面或 Vite
flag，启用后也只有 API/CLI JSON 结果。

### 信任边界与正常流程

普通 Web/CLI caller 不能创建 approval authority。完整正常流程分为两个独立步骤：

1. 受控 OS 操作员运行 `factory_core.phase78_operator`。该入口重验当前 Phase 3
   aggregate/occurrence 与 Phase 6 exact proof，持久化三角色原始 bytes，稳定读取
   revision-bound PDF，将 raw PDF、PNG、text、chunks、package 和 receipt 先写入并
   回读 CAS，再生成 durable trusted preflight。
2. Phase 6 grant 的 subject 把返回的 `trusted_preflight_sha256` 放入同一规范 request，
   再用 CLI 或 ACL-first Web submit/`run-one`。service、durable Scheduler/local worker
   重放并逐字段验证 preflight；Web 随后读取 effective status。

`PHASE78_TRUSTED_OPERATOR_ID` 和
`PHASE78_TRUSTED_OPERATOR_GENERATION` 只是受保护进程环境中的部署标签，不是
password、token、签名或身份验证器。信任来自可运行 operator 命令的受控 OS 账号，
以及 `0700` 的 store/CAS/spool/scratch 父目录和 `0600` 的持久文件。不要把该 OS
credential、operator 命令或可改写其环境的 shell 暴露给 Web 用户。operator issuer
必须不同于 Phase 6 subject；subject ID/generation 又必须与 exact grant 和认证 caller
一致。Phase 6 `snapshot:view` 证明仅是读取/currentness fence，不能升级为 egress
authority。

operator 命令与普通 CLI 是两套入口：

```bash
python3 -m factory_core.phase78_operator \
  --operator <trusted-operator-id> prepare <project-id> /abs/path/request.json

python3 -m factory_core.cli phase78 --actor <phase6-subject> \
  submit <project-id> /abs/path/request-with-preflight-hash.json
python3 -m factory_core.cli phase78 --actor <phase6-subject> \
  run-one <project-id> /abs/path/request-with-preflight-hash.json
python3 -m factory_core.cli phase78 --actor <phase6-subject> \
  status <project-id> <idempotency-key>
python3 -m factory_core.cli phase78 --actor <phase6-subject> \
  cancel <project-id> /abs/path/cancel-request.json
```

operator preparation 不入队、不启动 worker，也不 dispatch。普通 request 中自报的
issuer/approval 或伪造 preflight hash 不会获批。撤销只能由持久 approval 的 issuer
执行；具体 JSON schema 和 successor CAS 见架构合同与测试 fixture，不应从旧审计
报告复制。

### 启用配置

显式启用要求下列值全部有效；路径必须是绝对路径且分别指向受控的私有资源：

```dotenv
PHASE78_ENABLED=true
PHASE78_AUTHORITY_DB_FILE=/absolute/private/path/authority.db
PHASE78_AUTHORITY_SOURCE_FENCE_SHA256=<64-lowercase-hex>
PHASE78_PHASE6_DB_FILE=/absolute/private/path/phase6.db
PHASE78_PHASE7_DB_FILE=/absolute/private/path/phase7.db
PHASE78_PHASE8_DB_FILE=/absolute/private/path/phase8.db
PHASE78_WORK_DB_FILE=/absolute/private/path/work.db
PHASE78_WORK_SPOOL=/absolute/private/path/work-spool
PHASE78_PROJECT_ROOT=/absolute/private/path/project-root
PHASE78_CAS_ROOT=/absolute/private/path/cas
PHASE78_SCRATCH_ROOT=/absolute/private/path/scratch
PHASE78_DEADLINE_MS=30000
PHASE78_LEASE_SECONDS=30
PHASE78_TRUSTED_OPERATOR_ID=<trusted-operator-id>
PHASE78_TRUSTED_OPERATOR_GENERATION=<operator-generation>
```

deadline 范围是 1–300000ms，lease 范围是 1–86400 秒。一次 request 只创建一个
总 deadline，覆盖 Authority/Phase 6 current read、SQLite busy、file/PDF/CAS、
Phase 7/8 和最多一次 committed replay；每层不得重置预算。超时、用户取消、shutdown
和 superseded generation 有不同稳定 code。若内层已提交后才超时，结果是 uncertain；
caller 应用同一 idempotency key 查询/重放，不能换 key 重复生成。

Authority 中任何 project/run/runtime/Scheduler generation 仍是 `legacy_unknown` 时
必须先走正常生产 migration 并落地具体 generation。不要手改 Authority 行、伪造
generation 或放宽 Phase 6 eligibility 来启用 sidecar。

### Web API 与 current 语义

启用后的路由是：

```text
POST /api/projects/{base_name}/phase78-shadow/requests
POST /api/projects/{base_name}/phase78-shadow/run-one
GET  /api/projects/{base_name}/phase78-shadow/{idempotency_key}
POST /api/projects/{base_name}/phase78-shadow/{idempotency_key}/cancel
POST /api/projects/{base_name}/phase78-shadow/approvals/{approval_id}/revoke
```

每条路由都先完成认证和现有 `web/auth.db` 项目 ACL，再检查 flag/config、解析 body、
导入 core 或访问资源。Web 不提供 operator preflight。未知异常返回泛化安全文案；
可公开的 conflict/not-found/deadline/request code 才使用稳定映射。

历史 receipt 与当前可用性明确分离：旧 PASS/AUTHORIZED 事实仍可按不可变 identity
重放，但 Phase 3/6/7 head、work generation、approval lifecycle、expiry、successor 或
policy 漂移会让 effective current 返回 unavailable/`DENIED`。status 使用 service-owned
当前时间，所以重启后也不会继续暴露已过期 approval。成功结果仍固定
`authoritative=false`、`authority_transferred=false`、`dispatch_performed=false`、
`provider_call_performed=false`、`outbox_dispatch_performed=false`。

取消请求的 JSON 固定为 `phase78-work-cancel-request-v1`，并只接受与 URL
相同的 canonical idempotency key、逻辑 `cancelled_at` 和
`reason="user_cancel"`。内部 shutdown/superseded 分类同样持久化，但公共响应只返回
稳定的 reason、occurred-at 和 receipt hash；claim owner、epoch、nonce 等私有 lease
token 不进入 CLI/Web 响应。重启和同请求重放保留最初原因，不同原因或时间按
idempotency conflict 失败关闭。只有 durable `SUCCEEDED` work 才能把 Phase 8
decision 投影为 pipeline 成功；cancelled、failed、pending 或 active 状态不会被历史
`AUTHORIZED` decision 翻成成功。

PDF 只在 operator preparation 期间按 occurrence digest/length 稳定读取。完成 preflight
后，普通 worker 和重启 replay 使用持久 CAS components，不依赖原绝对 PDF 路径或
当前 parser 版本；missing/corrupt/encrypted/no-text 以稳定结构化 unavailable 结束，
不返回绝对路径或 500 内部细节。

### 验证与回滚

`./bootstrap.sh` 继续独立验证冻结的 Phase 3–6 精确 657 合同；
`./bootstrap_phase78.sh` 验证 Phase 7+8 的 unit、runtime、adapters、PDF/CAS 和真实
enabled E2E。Phase 7+8 当前精确 group/count 只以
`python3 -m scripts.phase78_test_contract describe` 为准，两套门禁都要求全部收集项
通过且零 skip/xfail/xpass。

回滚时把 `PHASE78_ENABLED` 设回 `false` 并重启后端。验收应确认 router 消失、普通
CLI/service/Scheduler/worker 在资源访问前返回、旧 Dashboard/Phase 1–6 输出不变。
不得删除 Phase 7/8 SQLite、CAS、spool、scratch 或历史 receipts；它们保留供审计和
后续同 key replay。完整运维前置、smoke 和回滚记录见
[`docs/deployment/DEPLOYMENT.md`](docs/deployment/DEPLOYMENT.md)。

完整身份、持久化、trust/currentness 和 no-dispatch 合同见
[`../docs/architecture/PHASE7_8_DURABLE_FULL_SHADOW.md`](../docs/architecture/PHASE7_8_DURABLE_FULL_SHADOW.md)。

## API 概览

公开只读：

- `GET /api/showcase/papers`
- `GET /api/showcase/papers/{base_name}/pdf`

认证：

- `POST /api/auth/register`
- `POST /api/auth/login`
- `GET /api/auth/me`
- `POST /api/auth/logout`
- `GET /api/showcase/user-papers`
- `GET /api/showcase/user-papers/{base_name}/pdf`

项目与申请：

- `POST /api/upload/problem`
- `GET /api/projects`
- `POST /api/projects/new`（管理员）
- `GET|POST /api/project-requests`
- `POST /api/admin/project-requests/{request_id}/approve`
- `POST /api/admin/project-requests/{request_id}/reject`
- `POST /api/projects/{base_name}/action`
- `GET /api/projects/{base_name}/contest-dashboard`
- `GET /api/projects/{base_name}/modeling-directions`（分层方法召回与结构化内容块）
- `GET /api/projects/{base_name}/problem-plan`（经校验的问题专属 DAG）
- `GET /api/projects/{base_name}/phase6-snapshot`（默认关闭、ACL-first、非权威只读 shadow）
- `POST /api/projects/{base_name}/phase78-shadow/requests`（默认关闭、ACL-first、durable shadow submit）
- `POST /api/projects/{base_name}/phase78-shadow/run-one`（默认关闭、显式同步 local worker）
- `GET /api/projects/{base_name}/phase78-shadow/{idempotency_key}`（默认关闭、effective current read）
- `POST /api/projects/{base_name}/phase78-shadow/approvals/{approval_id}/revoke`（默认关闭、issuer-only shadow revoke）
- `GET /api/projects/{base_name}/submission`（仅验证过的 current release ZIP）

管理员：

- `GET /api/admin/users`
- `POST /api/admin/users/{username}/approve|reject|disable`
- `DELETE /api/admin/users/{username}`
- `GET /api/admin/showcase`
- `PUT /api/admin/showcase/audiences/{audience_id}`
- `GET /api/admin/ops/secrets`
- `GET /api/admin/audit-log`
- `GET|POST /api/admin/delivery-overrides`
- `POST /api/admin/delivery-overrides/{override_id}/revoke`

交付授权只由管理员签发并持久化到 `web/auth.db`。`continue_after_gate2`
仅允许继续生成最终内容；`deliver_snapshot` 必须绑定精确的 64 位小写
SHA-256，才能授权该 Final Audit 快照交付。项目目录中的
`gate2_delivery_override.json` 不具备授权能力，真实 verdict 始终保留。

其余项目详情、文件、日志、咨询、选择和模型配置接口均要求认证并执行项目 ACL 或管理员校验。

## 代码结构

```text
web/
├── backend/
│   ├── main.py           # FastAPI 主应用
│   ├── app.py            # 兼容启动器/重导出
│   ├── auth_store.py     # SQLite 用户、审批、ACL、审计
│   ├── project_api.py    # 上传、项目、咨询、选择、模型 API
│   ├── phase6_api.py     # 默认关闭的 ACL-first Phase 6 只读适配器
│   └── start.sh
├── frontend/
│   ├── src/
│   └── package.json
├── auth.db               # 本地运行态，gitignored
├── start_dashboard.sh
└── deploy.sh
```

## 聚焦验证

从仓库根目录运行：

```bash
.venv/bin/python -m pytest -q \
  tests/test_web_control_plane_api.py \
  tests/test_web_frontend_runtime_helpers.py \
  tests/test_phase6_snapshot_grants.py \
  tests/test_phase6_project_snapshot_ui.py \
  tests/test_phase6_web_integration.py

bash -n scripts/load_secrets.sh scripts/setup_secret_manager.sh \
  web/backend/start.sh web/backend_service_health.sh web/deploy.sh

(cd web/frontend && npm run build)
(cd web/frontend && VITE_PHASE6_FULL_SHADOW_ENABLED=true npm run build)
```

完整仓库 pytest 可能受历史测试重名和可选 Web/runtime 依赖影响；优先使用与变更合同对应的聚焦测试。

## 生产运维

生产服务、nginx、Secret Manager、部署、live smoke 和回滚步骤只以 [`docs/deployment/DEPLOYMENT.md`](docs/deployment/DEPLOYMENT.md) 为准。不要从旧“部署完成”报告复制命令或凭据。
