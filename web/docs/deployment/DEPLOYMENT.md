# Web Dashboard 生产部署与回滚

本文是当前唯一现役的 Web 生产 runbook。日期化的“部署完成/确认/总结”文件仅保留历史证据，不得作为命令或凭据来源。

## 生产拓扑

```text
https://tfisher.de
        │
        ├── nginx → /var/www/tfisher.de/（Vite 静态文件）
        ├── /api  → 127.0.0.1:8000（FastAPI）
        └── /ws   → 127.0.0.1:8000/ws

paper-factory-api.service
        └── /home/tfisher/paper_factory/.venv/bin/uvicorn apps.web.backend.main:app
            └── scripts/load_secrets.sh → GCP Secret Manager
```

当前后端架构入口是 `web/backend/main.py`；`web/backend/app.py` 仅是兼容启动器和重导出模块。systemd 服务以 `tfisher` 用户运行，认证数据位于 `web/auth.db`（SQLite）。

## 部署前置条件

- 当前分支已完成代码审查和聚焦测试；不要从 dirty worktree 直接发布未经确认的变更。
- `gcloud` CLI、GCP 项目和 Secret Manager IAM 可用。
- 必需 secret（MinerU、Gemini、DeepSeek、JWT、管理员密码）已存在；只验证元数据/访问状态，不打印值或片段。
- `web/.env` 只含非敏感运行配置，例如 `GCP_PROJECT_ID`、`CORS_ORIGINS` 和首次升级时用于初始化访客展示集合的 `SHOWCASE_PROJECTS`。初始化后展示权限由管理员页面和 `web/auth.db` 持久化；敏感键会使部署预检失败。
- Phase 6 验证快照是单独审批的 default-off full-shadow 能力。普通部署保持
  `PHASE6_SNAPSHOT_ENABLED=false` 且不把
  `VITE_PHASE6_FULL_SHADOW_ENABLED` 设为精确 `true`；不能因为代码存在就推断
  已获准上线。默认构建通过 inert virtual-module alias 完全排除 Phase 6 面板、
  route 和客户端；仅用运行时 CSS/`v-if` 隐藏不符合回滚合同。
- Phase 7+8 durable local sidecar 也需要单独审批。普通部署保持
  `PHASE78_ENABLED=false`；默认进程不注册其 Web router、不读取 request/path、
  不创建 SQLite/CAS/spool、不导入重模块，也不启动 thread/process。它没有前端
  bundle、provider、outbox 或 dispatch，不能因源码存在或本地 shadow test 通过
  就推断已获 cutover/部署授权。
- 前端构建由服务用户执行，避免 root-owned `dist/` 阻塞下一次构建。
- 根 `.venv` 已由 `uv sync --extra web --extra models --locked` 准备；部署时不会安装 Python 依赖。
- `pyproject.toml`、`uv.lock`、两个 requirements lock export 和前端 `package-lock.json` 完整且已审查。

## 标准部署

在仓库根目录执行：

```bash
cd /home/tfisher/paper_factory
sudo -u tfisher -H /home/tfisher/google-cloud-sdk/bin/gcloud auth list
sudo -u tfisher -H /home/tfisher/.local/bin/uv sync --extra web --extra models --locked
sudo install -m 0644 deploy/systemd/paper-factory-api.service \
  /etc/systemd/system/paper-factory-api.service
sudo systemctl daemon-reload
sudo ./web/deploy.sh
```

`deploy.sh` 会依次：

1. 对 shell 脚本执行 `bash -n`；
2. 检查 Python/Web/Cloud/frontend 锁文件和原生 Step 0-16 registry；
3. 检查 `.env` 没有敏感键且权限不过宽；
4. 以服务用户预检 Secret Manager loader；
5. 检查 live systemd unit 使用仓库根目录、根 `.venv` 和稳定 ASGI 入口；
6. 以服务用户运行 `npm ci` 和 `npm run build`；
7. 将 `dist/` 同步到 `/var/www/tfisher.de/` 并设置静态文件权限；
8. 重启 `paper-factory-api.service`；
9. 确认 unit 为 `active/running`、`MainPID` 与所有 8000 listener 都属于
   unit 的 `ControlGroup`，并在稳定窗口内保持 PID 与 `NRestarts` 不变；
10. 再验证本地 API、canonical HTTPS 和前端指纹，任一不一致都以非零状态失败。

只更新后端：

```bash
cd /home/tfisher/paper_factory
sudo ./web/deploy.sh backend-only
```

不要手动 `rm -rf` 生产目录，也不要以 root 构建前端后再把产物留在仓库中。

## 可选 Phase 6 full-shadow 启停

本节只定义已获单独发布批准后的配置和回滚方法；它不构成当前候选的部署授权。
Phase 6 HTTP 边界是认证且只读的，仍先执行现有项目 ACL。它不替代
`web/auth.db`、项目 `.factory/state.db`、Authority writer 或 delivery override，
也不会 dispatch。

### 启用前条件

- 独立 Phase 6 SQLite 已由受审查 producer/harness 在目标主机上建立并含有预期
  项目的 current verified snapshot；Web 只读取，不创建或填充该库。
- 路径是绝对路径，父目录真实存在且不经过符号链接；store 文件是单链接普通文件、
  mode `0600`，并通过 Phase 6 精确 schema/marker/integrity 校验。
- 数据库不复用 `web/auth.db`、项目 `.factory/state.db` 或 Authority 数据库。
- 已记录数据库备份/身份、目标 commit、获批项目范围和回滚负责人。

在 `web/.env` 中设置后端非敏感项。推荐显式使用生产绝对路径；若省略路径，
程序默认解析为 `/home/tfisher/paper_factory/run_state/phase6_snapshot_shadow.db`：

```dotenv
PHASE6_SNAPSHOT_ENABLED=true
PHASE6_SNAPSHOT_DB_FILE=/home/tfisher/paper_factory/run_state/phase6_snapshot_shadow.db
```

在 gitignored 的 `web/frontend/.env.production.local` 中设置构建期 gate：

```dotenv
VITE_PHASE6_FULL_SHADOW_ENABLED=true
VITE_PHASE6_SNAPSHOT_DEADLINE_MS=15000
```

前端值必须是精确小写字符串 `true`；后端 bool parser 接受其列明的标准 true/
false 值并拒绝其他字符串。request deadline 必须是 1–300000ms 的正整数，默认
15000ms；它同时约束 headers、body 和一次 stale retry，修改后必须重建前端。
两侧必须一致启用，然后按“标准部署”完整构建与重启。
`backend-only` 不能把尚未启用的前端变成 Phase 6 UI。

### Phase 6 live smoke

除通用 smoke 外，使用不记录 token 值的测试账号/会话验证：

- 未认证请求在任何 store 访问前被拒绝；
- 没有目标项目 ACL 的 active 用户仍得到项目不可见响应，不能探测 Phase 6 flag
  或数据库状态；
- 对同一无 ACL 用户，省略、传负数或传非整数 `expected_revision` 都保持同一项目
  拒绝；只有已授权用户的非法 revision 才返回参数 4xx；
- 有目标项目 ACL 的用户能打开“验证快照”，响应 schema 是
  `phase6-project-snapshot-web-v1`，project/snapshot/revision 一致，三个 safety
  bit 全为 false；
- 带旧 `expected_revision` 的请求得到 409，页面至多重读一次；
- 模拟服务不返回 headers 及返回 headers 后 body 不完成，两种情况下页面都在配置的
  总 deadline 内清除 `aria-busy`、恢复刷新按钮并聚焦安全错误；迟到响应不覆盖错误；
- 无 snapshot、PARTIAL/ineligible source、篡改/不一致 store 和内部异常失败
  关闭，公共错误不包含绝对路径、SQL 或内部异常消息；
- 页面不改变项目 revision，不写任何生产数据库，不执行 action 或 dispatch。

### Phase 6 回滚

回滚不需要也不得删除 shadow SQLite。在 `web/.env` 中改为：

```dotenv
PHASE6_SNAPSHOT_ENABLED=false
```

并在 `web/frontend/.env.production.local` 中改为：

```dotenv
VITE_PHASE6_FULL_SHADOW_ENABLED=false
```

随后执行标准完整部署，使前端重新构建并重启后端。验收要求是 v1 页面保持正常、
Phase 6 标签消失、已授权 API 调用在 ACL 检查后返回 disabled 404、后端没有导入
Phase 6 core 或访问 store。保留数据库及启停前后身份供审计。仅关闭前端会隐藏 UI
但仍留下已启用 API；仅关闭后端会让已构建 UI 显示 unavailable，因此都不是完整
回滚。

## 可选 Phase 7+8 durable local sidecar 启停

本节只定义候选在获得单独批准后的本地 sidecar 配置、正常流程 smoke 与回滚；
它不批准当前候选上线、production cutover、provider dispatch、upload 或 authority
transfer。Phase 7+8 是同步 local `run-one`，没有后台 Scheduler/worker、生产 outbox
或 Phase 7+8 前端页面。

### 信任与启用前条件

- 运行 `factory_core.phase78_operator` 的 OS 账号必须是受控本地账号；在当前单用户
  拓扑中，服务账号必须被当作可信 operator credential 管理，不能向 Web 用户提供
  shell、任意命令、环境改写或 operator RPC。
- `PHASE78_TRUSTED_OPERATOR_ID` 和
  `PHASE78_TRUSTED_OPERATOR_GENERATION` 只是受保护部署环境中的审计标签，不是
  password、token、signature 或身份验证器。request JSON 不能选择信任根。
- operator issuer 必须不同于 Phase 6 grant subject。subject ID/generation 必须与
  当前 exact Phase 6 proof 和后续认证 CLI/Web caller 一致；`snapshot:view` 本身
  不能授权 egress。
- 所有运行父目录预先存在、真实且私有，mode 为 `0700`；SQLite、canonical job
  等持久文件为 `0600`，CAS 已发布 blob 为不可变只读。路径不得复用
  `web/auth.db`、项目 `.factory/state.db`、生产 outbox 或其他 phase store。
- Authority、Phase 6 和 project root 已备份并记录 identity。Authority 的 project、
  run、runtime、Scheduler generation 均为正规 migration 产生的具体值；任一仍是
  `legacy_unknown` 时不得启用，也不得手改行或伪造 generation。
- 源码树先通过 `./bootstrap.sh` 的冻结 657 合同和独立
  `./bootstrap_phase78.sh`。Phase 7+8 当前 exact group/count 只从
  `python3 -m scripts.phase78_test_contract describe` 读取，不在部署记录中复制旧值。
- 已记录目标 commit、Authority source fence、获批项目/subject/operator scope、
  私有路径 inventory、启用窗口和回滚负责人。

### 配置

在受控的非公开服务环境设置以下非敏感配置；所有路径都必须是绝对路径。示例名称
只表示布局，不是预先存在的生产值：

```dotenv
PHASE78_ENABLED=true
PHASE78_AUTHORITY_DB_FILE=/srv/paper-factory/phase78/authority.db
PHASE78_AUTHORITY_SOURCE_FENCE_SHA256=<64-lowercase-hex>
PHASE78_PHASE6_DB_FILE=/srv/paper-factory/phase78/phase6.db
PHASE78_PHASE7_DB_FILE=/srv/paper-factory/phase78/phase7.db
PHASE78_PHASE8_DB_FILE=/srv/paper-factory/phase78/phase8.db
PHASE78_WORK_DB_FILE=/srv/paper-factory/phase78/work.db
PHASE78_WORK_SPOOL=/srv/paper-factory/phase78/work-spool
PHASE78_PROJECT_ROOT=/srv/paper-factory/phase78/project-root
PHASE78_CAS_ROOT=/srv/paper-factory/phase78/cas
PHASE78_SCRATCH_ROOT=/srv/paper-factory/phase78/scratch
PHASE78_DEADLINE_MS=30000
PHASE78_LEASE_SECONDS=30
PHASE78_TRUSTED_OPERATOR_ID=<trusted-operator-id>
PHASE78_TRUSTED_OPERATOR_GENERATION=<operator-generation>
```

`PHASE78_DEADLINE_MS` 范围为 1–300000，`PHASE78_LEASE_SECONDS` 范围为
1–86400。一次请求的 deadline 同时覆盖 current-head/SQLite busy、文件/PDF/CAS、
Phase 7/8 和一次 committed replay，不能在各 adapter 重置。不要把上述标签放进
request body，也不要把它们误记为 secret 轮换对象；需要保护的是 OS credential、
服务配置写权限和私有目录。

配置后按“标准部署”重启后端。由于 Phase 7+8 没有 frontend，前端重新构建不会
新增 Phase 7+8 页面；router 只在后端进程启动时按 flag 注册。

### 可信 preparation 与普通执行

对每个获批 request，先由受控 OS operator 运行：

在已安全加载上述配置的受控 `tfisher` 账号会话中运行；不要把环境值写到命令行：

```bash
cd /home/tfisher/paper_factory
/home/tfisher/paper_factory/.venv/bin/python \
  -m factory_core.phase78_operator --operator <trusted-operator-id> \
  prepare <project-id> /absolute/private/path/request.json
```

该命令重验当前 Phase 3/6 heads，持久化三角色 bytes，稳定读取 occurrence-bound
PDF，发布并回读 CAS package，再输出 `trusted_preflight_sha256`。它不入队、不启动
worker、不调用 provider、不 dispatch。把该 hash 写入同一 canonical request 后，
Phase 6 subject 才可用普通 CLI `submit`/`run-one`，或通过已授权 Web API 执行。
operator 命令不应由 Web endpoint、通用 job runner 或 request hook 代跑。

### Phase 7+8 live smoke

除通用 smoke 外，使用不记录 token 值的测试账号/会话验证：

- 默认关闭进程没有 Phase 7+8 route/import/resource；启用后，未认证与无项目 ACL
  用户都在 flag、body、path、database/CAS 和 lazy core 之前被拒绝；
- operator preflight 的 issuer 与 subject 不同，subject/generation 与 Phase 6 proof
  精确一致；伪造、缺失或跨 request preflight hash 失败关闭；
- 真实路径按 operator preflight → CLI/service → durable Scheduler/local worker →
  Phase 7 store → PDF/CAS/Phase 8 → authenticated Web status 完成；
- 相同 idempotency key/bytes 并发和重启 replay 收敛，同 key 不同 bytes 冲突；
  timeout 后 uncertain 只用原 key 查询/重放；
- 移动原三角色 packet root 或在 successful preflight 后移除原 PDF，持久 replay
  identity 不变；missing/corrupt/encrypted/no-text 在 preparation 阶段给稳定结构化
  unavailable，不泄漏绝对路径或内部堆栈；
- Phase 3/6/7 head、work generation、policy、approval lifecycle 或 server-time expiry
  变化后，历史 receipt 仍保留而 effective current 立即 unavailable/`DENIED`；
- approval revoke 后 Web status 为 `DENIED`；旧 AUTHORIZED 只作为 historical
  receipt，不作为当前授权；
- 全部响应的 `authoritative`、`authority_transferred`、`dispatch_performed`、
  `provider_call_performed` 和 `outbox_dispatch_performed` 均为 `false`，进程未导入/
  调用 provider，未创建生产 outbox 或外部网络请求。

### Phase 7+8 回滚

在受控环境把 `PHASE78_ENABLED` 改为 `false`，然后重启后端。验收要求：Phase 7+8
router 不再注册；普通 CLI/service/Scheduler/worker 在 request path、数据库/CAS 和
重模块 import 前返回 disabled；Phase 1–6 页面、API、CLI 和输出保持原样。

回滚不得删除或改写 Phase 7/8 SQLite、work database/spool、CAS、scratch、operator
preflight 或历史 receipts。保留启停前后 commit、flag、source fence、路径/mode、
route inventory 和 smoke 结果。旧 work 可在以后重新获批启用后用同 key 审计或
重放；关闭 flag 本身不声称撤销历史事实，也不产生 dispatch。

## 预检与验证

### 本地服务

```bash
sudo ./web/backend_service_health.sh --verify
systemctl is-active nginx.service
curl -fsS http://127.0.0.1:8000/
```

健康脚本输出 `MainPID|NRestarts|ControlGroup|listener PIDs`。单独看到
`systemctl active` 或 HTTP 200 都不算成功：旧会话遗留的 rogue listener
可能继续响应端口，必须证明所有 listener 都在正式 unit cgroup 内。预期 API
响应是状态对象；不应在输出中出现 secret。若服务启动失败，先看：

```bash
sudo journalctl -u paper-factory-api.service -n 100 --no-pager
```

### 用户面

```bash
curl -kfsS -I https://tfisher.de/
curl -kfsS https://tfisher.de/ >/dev/null
```

随后用浏览器验证：

- 未登录只能看到公开论文展厅；
- 登录/注册与管理员审批路径可用；
- `/api/projects` 返回题目归档字段 `problem_key`、`problem_title`、`storage_scope`、`archived`；
- 同题多次运行在 UI 中聚合，但原始目录仍在 `ongoing/` 或 `complete/`；
- WebSocket、日志、咨询、Step 3 选择和项目 ACL 与当前用户权限一致。
- 默认部署不出现“验证快照”标签；若 Phase 6 经批准启用，则追加执行本 runbook
  的 ACL-first、revision 与 false-safety-bit smoke。
- 默认部署没有 Phase 7+8 route 或资源访问；若 Phase 7+8 经批准启用，则追加执行
  本 runbook 的 operator-preflight、ACL-first、durable replay、effective-current 与
  全 false safety-bit smoke。Phase 7+8 没有可检查的 frontend 面板。

### 构建指纹

```bash
sha256sum web/frontend/dist/index.html /var/www/tfisher.de/index.html
```

两者一致只能证明当前文件内容一致；仍需结合 canonical URL 响应、systemd active 状态和发布时间判断 live 状态。

完整部署必须同时满足：源码 `dist/index.html` 与生产文件指纹一致、canonical
HTTPS 可访问、后端 listener 所有权正确且稳定。`backend-only` 不重发前端，
但仍使用完全相同的后端 cgroup/PID/重启稳定性验收。

## 回滚

回滚前先记录当前 commit、服务状态和前端指纹。优先回到已审查的 Git commit，再按标准部署流程构建和重启：

```bash
cd /home/tfisher/paper_factory
git status --short --branch
git log -1 --oneline
# 由发布负责人选择目标已审查 commit 后，再执行常规 checkout/构建流程
sudo ./web/deploy.sh
```

不要用历史报告中的 `git checkout HEAD~1`、旧 systemd 服务名或旧部署目录作为盲回滚命令。若只需恢复后端，使用 `backend-only` 并重新运行本地/线上 smoke。

Secret 轮换、旧版本禁用、备份删除、停服和删除 worktree 都是独立的高风险操作；本 runbook 不会替用户隐式执行。

## 故障处理

### Secret loader 失败

确认服务用户能找到 `gcloud`、`GCP_PROJECT_ID` 非空、IAM 允许读取所需 secret。只检查 secret 名称、版本状态和访问返回码，不打印 payload。

### 后端重启失败

```bash
systemctl status paper-factory-api.service --no-pager
sudo journalctl -u paper-factory-api.service -n 200 --no-pager
sudo ./web/backend_service_health.sh --verify
sudo ss -H -ltnp 'sport = :8000'
```

若 8000 listener 不属于 `paper-factory-api.service` 的 ControlGroup，先记录
准确 PID、命令行和 cgroup，再停止对应的遗留会话进程；不要用 HTTP 200 掩盖
正式 unit 启动失败。仓库 unit 使用 `KillMode=control-group`、停止超时和
SIGKILL 收尾，并有启动限流，避免后续重启再次遗留子进程或无限抖动。修复配置后
重新执行预检；不要通过弱默认密码或自动生成 JWT 绕过启动校验。

### 首页仍是旧版本

同时比较 `web/frontend/dist/index.html`、`/var/www/tfisher.de/index.html` 和 canonical HTTPS 响应。确认 nginx 仍指向 `/var/www/tfisher.de`，再执行一次标准部署；cache-buster 只能辅助诊断，不能替代 live 验收。

### API/静态文件路径异常

检查 `/etc/nginx/sites-available/tfisher.de` 中的 `/api`、`/ws` 和 `/` location，并运行 `sudo nginx -t` 后再 reload。不要把旧 `/paper-factory/` 子路径报告当作当前域名合同。

### Phase 6 启用后失败

先确认前后端 flag 是否匹配，以及 systemd 实际读取的
`PHASE6_SNAPSHOT_DB_FILE` 是绝对路径。缺失 store、普通/多链接文件、错误 mode、
符号链接父目录、未知 sidecar、foreign schema、marker/hash/coordinate 不一致都会
失败关闭；不要用 `chmod`、复制空库或绕过校验来“修复”。记录数据库身份与日志中
的错误类型，回滚双 flag，再由 Phase 6 store 审核流程诊断。公共 API 的泛化错误
是预期的信息泄漏边界，不应改成返回内部路径或 SQL。

### Phase 7+8 启用后失败

先把故障分类为 config、operator preflight、durable work、Phase 7 grounding、PDF/CAS、
Phase 8 currentness 或 Web ACL，而不是重复生成 idempotency key。确认服务实际读取的
全部路径均为受控绝对路径，parent/file mode 符合 `0700`/`0600`，Authority source
fence 与 concrete generations 正确，且 operator issuer、Phase 6 subject 和各
generation 没有混用。`legacy_unknown` 必须由正规 migration 关闭，不能靠 SQL 手改。

如果请求可能已经提交，先用原 idempotency key 查询 status；post-commit timeout 的
uncertain 结果只能用该 key 重放。missing/corrupt/encrypted/no-text PDF、CAS digest/
length 失败、stale head、expired/revoked/superseded approval 都应失败关闭；不要复制
原文件、放宽 schema/hash、重置 current row 或把公共错误改成泄漏内部路径。无法在
批准窗口内恢复时关闭 `PHASE78_ENABLED`、重启并按本节回滚验收；保留所有 store、
CAS、spool、preflight、原始日志和身份供审计。

## 运行后记录

发布记录至少保留：目标 commit、部署命令结果、systemd active 时间、前端指纹、canonical URL smoke、是否回滚，以及任何未验证项。发布状态应分别写 `implemented`、`deployed`、`live verified`、`knowledge closed`；不要用“完成”覆盖缺失证据。
