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
        └── /home/tfisher/paper_factory/web/backend/venv/bin/uvicorn app:app
            └── scripts/load_secrets.sh → GCP Secret Manager
```

当前后端架构入口是 `web/backend/main.py`；`web/backend/app.py` 仅是兼容启动器和重导出模块。systemd 服务以 `tfisher` 用户运行，认证数据位于 `web/auth.db`（SQLite）。

## 部署前置条件

- 当前分支已完成代码审查和聚焦测试；不要从 dirty worktree 直接发布未经确认的变更。
- `gcloud` CLI、GCP 项目和 Secret Manager IAM 可用。
- 必需 secret（MinerU、Gemini、DeepSeek、JWT、管理员密码）已存在；只验证元数据/访问状态，不打印值或片段。
- `web/.env` 只含非敏感运行配置，例如 `GCP_PROJECT_ID`、`CORS_ORIGINS` 和 `SHOWCASE_PROJECTS`。敏感键会使部署预检失败。
- 前端构建由服务用户执行，避免 root-owned `dist/` 阻塞下一次构建。

## 标准部署

在仓库根目录执行：

```bash
cd /home/tfisher/paper_factory
sudo -u tfisher -H gcloud auth list
sudo ./web/deploy.sh
```

`deploy.sh` 会依次：

1. 对 shell 脚本执行 `bash -n`；
2. 检查 `.env` 没有敏感键且权限不过宽；
3. 以服务用户预检 Secret Manager loader；
4. 以服务用户运行 `npm run build`；
5. 将 `dist/` 同步到 `/var/www/tfisher.de/` 并设置静态文件权限；
6. 重启 `paper-factory-api.service`；
7. 重试本地 API 和 canonical HTTPS 首页，后端无法就绪时以非零状态失败。

只更新后端：

```bash
cd /home/tfisher/paper_factory
sudo ./web/deploy.sh backend-only
```

不要手动 `rm -rf` 生产目录，也不要以 root 构建前端后再把产物留在仓库中。

## 预检与验证

### 本地服务

```bash
systemctl is-active paper-factory-api.service
systemctl is-active nginx.service
curl -fsS http://127.0.0.1:8000/
```

预期 API 响应是状态对象；不应在输出中出现 secret。若服务启动失败，先看：

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

### 构建指纹

```bash
sha256sum web/frontend/dist/index.html /var/www/tfisher.de/index.html
```

两者一致只能证明当前文件内容一致；仍需结合 canonical URL 响应、systemd active 状态和发布时间判断 live 状态。

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
```

修复配置后重新执行预检；不要通过弱默认密码或自动生成 JWT 绕过启动校验。

### 首页仍是旧版本

同时比较 `web/frontend/dist/index.html`、`/var/www/tfisher.de/index.html` 和 canonical HTTPS 响应。确认 nginx 仍指向 `/var/www/tfisher.de`，再执行一次标准部署；cache-buster 只能辅助诊断，不能替代 live 验收。

### API/静态文件路径异常

检查 `/etc/nginx/sites-available/tfisher.de` 中的 `/api`、`/ws` 和 `/` location，并运行 `sudo nginx -t` 后再 reload。不要把旧 `/paper-factory/` 子路径报告当作当前域名合同。

## 运行后记录

发布记录至少保留：目标 commit、部署命令结果、systemd active 时间、前端指纹、canonical URL smoke、是否回滚，以及任何未验证项。发布状态应分别写 `implemented`、`deployed`、`live verified`、`knowledge closed`；不要用“完成”覆盖缺失证据。
