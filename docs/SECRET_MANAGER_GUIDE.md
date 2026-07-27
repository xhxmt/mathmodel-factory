# GCP Secret Manager 使用指南

本文是仓库当前的 Secret Manager 操作说明。它只描述 secret 名称、权限和验证方法，不显示任何 secret 值、值片段或可登录凭据。

## 当前合同

生产敏感值由 GCP Secret Manager 提供：

| 环境变量 | Secret 名称 | 用途 |
|---|---|---|
| `MINERU_TOKEN` | `mineru-token` | MinerU 题目解析 |
| `GEMINI_API_KEY` | `gemini-api-key` | Gemini/Antigravity 调用 |
| `DEEPSEEK_API_KEY` | `deepseek-api-key` | DeepSeek 兼容 API |
| `JWT_SECRET` | `dashboard-jwt-secret` | Dashboard token 签名 |
| `ADMIN_PASSWORD` | `dashboard-admin-password` | 管理员 bootstrap 与登录 |

`JWT_SECRET` 至少 32 个字符；管理员密码不得为空或使用弱默认值。后端拒绝不满足条件的启动配置。旧环境变量名 `JWT_SECRET_KEY` 仅作为兼容回退读取，生产配置应统一使用 `JWT_SECRET`。

`scripts/load_secrets.sh` 每次运行时从 Secret Manager 读取这些条目，并覆盖同名的旧环境变量。这样可以避免过期 `.env` 值悄悄成为生产来源。

## 初次配置

### 前置检查

```bash
gcloud auth login
gcloud config set project <GCP_PROJECT_ID>
gcloud services enable secretmanager.googleapis.com
```

确认当前账号和项目后，再运行向导：

```bash
cd /home/tfisher/paper_factory
./scripts/setup_secret_manager.sh
```

向导从本地配置读取待迁移的值，通过 stdin 传给 `gcloud secrets create`/`versions add`，不会把 payload 写入日志。它会生成权限为 `0600` 的备份和无敏感值模板；验证并完成轮换后，备份是否删除需单独确认。

### 手工创建或轮换

不要把 secret 值写入命令行参数、脚本、文档或 shell history。使用受控输入（例如 Secret Manager 控制台、受保护的 stdin 或组织批准的密钥管线）创建版本：

```bash
gcloud secrets create <secret-name> --replication-policy=automatic
gcloud secrets versions add <secret-name> --data-file=/path/to/protected-input
```

`/path/to/protected-input` 必须是临时、权限受限且完成后可审计删除的文件；不要把它提交到仓库。

## 应用加载

本地或生产启动前由 loader 负责注入：

```bash
cd /home/tfisher/paper_factory
source scripts/load_secrets.sh
```

只检查退出状态或非敏感的“loaded successfully”消息：

```bash
if source scripts/load_secrets.sh >/dev/null 2>&1; then
  echo "Secret Manager access succeeded"
else
  echo "Secret Manager access failed"
fi
```

不要使用 `env`、`echo`、shell tracing、日志或 HTTP 响应打印这些变量。 `web/backend/start.sh`、systemd 服务和 `run_paper.sh` 的运行时路径都应通过 loader 获取敏感值。

## 元数据与权限验证

以下命令只返回元数据，不读取 payload：

```bash
gcloud secrets list
gcloud secrets describe dashboard-jwt-secret
gcloud secrets versions list dashboard-jwt-secret
gcloud secrets get-iam-policy dashboard-jwt-secret
```

验证应用访问时，使用 `scripts/load_secrets.sh` 的退出状态或管理员面板中的元数据状态。管理员 API `/api/admin/ops/secrets` 只应返回 secret 名称、存在性、绑定和访问状态，不返回值或片段。

## 轮换与回滚

1. 在受控环境生成新值并添加新版本。
2. 以非敏感的版本号/时间记录变更。
3. 重启后端和需要该 secret 的 runner。
4. 运行本地 API、Dashboard 登录、公开展厅和必要的建模 smoke。
5. 确认所有 consumer 使用新版本后，再决定是否禁用旧版本。

轮换 JWT Secret 会使现有 token 失效；轮换管理员密码会同步 SQLite 中的 `admin` 哈希。任何禁用旧版本、删除版本、删除备份或撤销 IAM 的操作都属于高风险运维变更，先报告依赖和回滚路径，再取得明确批准。

## 泄露响应

如果历史文档、日志或命令历史曾包含可用值，应按“已暴露”处理：

1. 立即限制访问并保留审计证据；
2. 生成并添加新版本；
3. 重启所有 consumer；
4. 检查 Secret Manager 审计日志和仓库远端暴露面；
5. 在确认回滚路径后禁用旧版本；
6. 不要把新值写入 issue、PR、文档或聊天记录。

本仓库的文档清理不会自动完成轮换或删除操作。

## 故障排查

### `gcloud` 找不到

确认服务用户的 `PATH` 包含 Google Cloud SDK，或按现场规则设置 `GCLOUD_BIN`。不要把完整本地路径或凭据写入公共文档。

### `GCP_PROJECT_ID is required`

在非敏感的 `.env`/`web/.env` 或服务环境中设置项目 ID，并确认该项目与 secret 所在项目一致。

### 权限错误

为实际运行服务账号授予 `roles/secretmanager.secretAccessor`，然后只用 `describe`、IAM 元数据和 loader 退出状态复核；不要用读取 payload 来“调试”。

### 后端拒绝启动

检查 JWT Secret 长度和管理员密码强度，但不要打印值。修复 Secret Manager 版本后重新启动并查看：

```bash
sudo journalctl -u paper-factory-api.service -n 100 --no-pager
```
