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
        └── uvicorn apps.web.backend.main:app --host 127.0.0.1 --port 8000
            └── scripts/load_secrets.sh → GCP Secret Manager
```

当前后端架构入口是 `web/backend/main.py`；`web/backend/app.py` 仅是兼容启动器和重导出模块。systemd 服务以 `tfisher` 用户运行，认证数据位于 `web/auth.db`（SQLite）。

## 部署前置条件

- 当前分支已完成代码审查和聚焦测试；不要从 dirty worktree 直接发布未经确认的变更。
- `gcloud` CLI、GCP 项目和 Secret Manager IAM 可用。
- 必需 secret（MinerU、Gemini、DeepSeek、JWT、管理员密码）已存在；只验证元数据/访问状态，不打印值或片段。
- `web/.env` 只含非敏感运行配置，例如 `GCP_PROJECT_ID`、`CORS_ORIGINS` 和首次升级时用于初始化访客展示集合的 `SHOWCASE_PROJECTS`。初始化后展示权限由管理员页面和 `web/auth.db` 持久化；敏感键会使部署预检失败。
- 前端构建由服务用户执行，避免 root-owned `dist/` 阻塞下一次构建。
- 根 `.venv` 已由 `uv sync --extra web --locked` 准备；部署时不会安装 Python 依赖。
- `pyproject.toml`、`uv.lock`、Web 的 requirements lock export 和前端 `package-lock.json` 完整且已审查。

## 标准部署

在仓库根目录执行：

```bash
cd /home/tfisher/paper_factory
sudo -u tfisher -H /home/tfisher/google-cloud-sdk/bin/gcloud auth list
sudo -u tfisher -H /home/tfisher/.local/bin/uv sync --extra web --locked
sudo install -m 0644 deploy/systemd/paper-factory-api.service \
  /etc/systemd/system/paper-factory-api.service
sudo systemctl daemon-reload
sudo ./web/deploy.sh
```

`deploy.sh` 会依次：

1. 对 shell 脚本执行 `bash -n`；
2. 检查 Python/Web/frontend 锁文件和原生 Step 0-16 registry；
3. 检查 `.env` 没有敏感键且权限不过宽；
4. 以服务用户预检 Secret Manager loader；
5. 检查 live systemd unit 使用仓库根目录、根 `.venv` 和稳定 ASGI 入口；
6. 以服务用户运行 `npm ci` 和 `npm run build`；
7. 把 `dist/` 覆盖同步到 `/var/www/tfisher.de/`、只回收超过保留期（`STALE_ASSET_DAYS`，默认 14 天）的历史构建文件，并设置静态文件权限；
8. 重启 `paper-factory-api.service`；
9. 确认 unit 为 `active/running`、`MainPID` 与所有 8000 listener 都属于
   unit 的 `ControlGroup`，并在稳定窗口内保持 PID 与 `NRestarts` 不变；
10. 再验证本地 API、canonical HTTPS、前端指纹和首页 `Cache-Control: no-cache`，任一不一致都以非零状态失败。

只更新后端：

```bash
cd /home/tfisher/paper_factory
sudo ./web/deploy.sh backend-only
```

不要手动 `rm -rf` 生产目录，也不要以 root 构建前端后再把产物留在仓库中。

## 主流程范围

此部署只包含 Native FactoryEngine、SQLite 和 Stage 控制面。Authority/Phase 模块、
shadow UI/API 和专用启停脚本已拆至 `~/paper_new`；旧 Phase 环境变量不再启用任何路由。
本次源码拆分不迁移生产数据库，也不要求自动重启。常规发布仍使用以下预检与回滚流程。

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
- 主流程不包含实验快照标签或 Phase API 路由。

### 构建指纹

```bash
sha256sum web/frontend/dist/index.html /var/www/tfisher.de/index.html
```

两者一致只能证明当前文件内容一致；仍需结合 canonical URL 响应、systemd active 状态和发布时间判断 live 状态。

### 前端缓存契约

`index.html` 引用带 hash 的构建资源，所以它必须每次回源校验：

- `/`（以及任何回退到 `index.html` 的 SPA 路由）返回 `Cache-Control: no-cache`；
  否则浏览器会按启发式缓存继续使用旧 `index.html`，长时间运行旧构建，并去请求
  新部署已经删除的 chunk。
- `/assets/*.js`、`*.css` 等带 hash 资源保持 `expires 1y` + `public, immutable`。
- `deploy.sh` 不再清空 `$WEB_ROOT`：旧 hash 资源保留 `STALE_ASSET_DAYS`（默认
  14 天），让仍在运行旧 `index.html` 的浏览器还能取到旧 chunk。

```bash
curl -sI https://tfisher.de/ | grep -i cache-control                    # 期望 no-cache
curl -sI https://tfisher.de/assets/index-<hash>.js | grep -i cache-control
```

前端还有两道自愈：入口脚本 404 时 `index.html` 内联脚本做一次受保护刷新；
懒加载 chunk 失败时 `vite:preloadError` 触发一次受保护刷新（同一入口 bundle 只
重试一次），失败后显示「界面资源加载失败」而不是永久转圈。

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

### 控制台卡在全屏转圈 / 只有深色背景

症状是页面一直停在全屏 loading（`.overlay-loading`），或者登录后立刻变成空白：

1. DevTools Console 若出现 `Failed to fetch dynamically imported module: .../assets/<Chunk>-<hash>.js`，且 Network 里该请求 404，说明浏览器仍在运行旧入口 bundle，而对应 chunk 已被新部署替换。
2. 先让用户强制刷新（Ctrl+Shift+R）或清站点缓存；正常刷新已经由 `vite:preloadError` 自愈兜底。
3. 再核对 `/` 的 `Cache-Control` 是否为 `no-cache`，以及 `/var/www/tfisher.de` 是否保留了上一轮构建的 hash 资源（见「前端缓存契约」）。旧 runbook 的 `rm -rf "$WEB_ROOT"/*` 会删除旧 chunk，是这类故障的成因，不要恢复该做法。

### 工作台大面积「请求超时」/ 面板长时间加载

工作台会每 8 秒轮询 `/steps`、`/diagnostics`、`/contest-dashboard`。这些接口在
大项目上会做审计重算（judge packet 指纹、decision receipt 哈希、workflow replay），
必须是「慢但只占线程池」：

1. 不要在 `async def` handler 里直接调用这些同步重算函数；用
   `run_in_threadpool(...)`，否则一个慢项目会冻结整个事件循环，让 `/problem-plan`
   这类毫秒级接口也一起超时。
2. 只消费 `current_step` / `is_running` / `consultation_*` 的调用方必须传
   `include_fingerprint=False`；`submission_fingerprint` 在大项目上要十几秒且只
   产生证据/交付字段。
3. 前端对 `/status`、`/diagnostics`、`/contest-dashboard` 使用 60s 审计超时
   （`web/frontend/src/lib/api.js` 的 `AUDIT_TIMEOUT_MS`），不要退回 15s 全局上限。

对照测量（`cumcm_2026_a_fable_pro_20260910`，4.4G / 4019 文件）：`/steps` 12.1s → 0.08s，
`/diagnostics` 25.1s → 11.1s（移出事件循环），重压下 `/problem-plan` 24.3s → 1.7s。

### API/静态文件路径异常

检查 `/etc/nginx/sites-available/tfisher.de` 中的 `/api`、`/ws` 和 `/` location，并运行 `sudo nginx -t` 后再 reload。不要把旧 `/paper-factory/` 子路径报告当作当前域名合同。

## 运行后记录

发布记录至少保留：目标 commit、部署命令结果、systemd active 时间、前端指纹、canonical URL smoke、是否回滚，以及任何未验证项。发布状态应分别写 `implemented`、`deployed`、`live verified`、`knowledge closed`；不要用“完成”覆盖缺失证据。
