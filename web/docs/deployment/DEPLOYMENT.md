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

## 运行后记录

发布记录至少保留：目标 commit、部署命令结果、systemd active 时间、前端指纹、canonical URL smoke、是否回滚，以及任何未验证项。发布状态应分别写 `implemented`、`deployed`、`live verified`、`knowledge closed`；不要用“完成”覆盖缺失证据。
