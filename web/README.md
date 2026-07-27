# Paper Factory Web Dashboard

本目录提供 Modeling Factory 的 Web 控制面。它负责公开论文展厅、用户与项目审批、项目运行监控、日志/文件查看、人工咨询和 Step 3 方案选择；建模流程本身仍由仓库根目录的 `launch_agents.sh` 与 `run_paper.sh` 执行。

当前文档分工：

- 本文：功能、权限、开发启动和 API 概览。
- [`QUICKSTART.md`](QUICKSTART.md)：最短本地启动路径。
- [`USAGE_GUIDE.md`](USAGE_GUIDE.md)：面向访客、普通用户和管理员的操作流程。
- [`docs/deployment/DEPLOYMENT.md`](docs/deployment/DEPLOYMENT.md)：唯一现役生产部署与回滚 runbook。

`docs/` 下的其他部署、测试和功能完成报告都是日期化历史快照，不能替代上述现役文档。

## 当前能力

- 未登录访客只能浏览 `SHOWCASE_PROJECTS` 白名单中的完成论文及 PDF。
- 用户可注册账号；新账号默认是 `pending`，管理员审批后才能登录。
- 认证和审批状态持久化在 `web/auth.db`，密码使用 bcrypt 哈希。
- 普通用户提交项目申请，管理员审批后创建项目并写入项目 ACL；普通用户只能看到获授权项目，管理员可见全部项目。
- 管理员可直接创建项目，并管理用户、项目申请、Secret Manager 元数据状态和审计日志。
- Dashboard 将题目内容相同的多次运行按 canonical SHA-256 身份聚合为一个“题目归档”。这只是展示层分组，不移动或改名 `ongoing/`、`complete/` 中的目录。
- 进行中的运行可暂停、恢复或终止；完成归档保持只读。
- 人工咨询和 Step 3 方法选择可在 Web 中完成。CLI 路径始终保留，见下文。

## 本地启动

### 前置条件

- Python 3、Node.js/npm、`gcloud` CLI。
- 当前 GCP 项目中已配置 `scripts/load_secrets.sh` 使用的 Secret Manager 条目。
- 当前账号有对应 Secret 的访问权限。

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

访客进入公开论文展厅，只能读取 `SHOWCASE_PROJECTS` 中存在于 `complete/` 的项目及其最终 PDF，不能查看内部项目、日志或控制动作。

### 普通用户

1. 在登录界面注册账号。
2. 等待管理员审批；`pending`、`rejected` 或 `disabled` 用户不能登录。
3. 上传 PDF、Markdown 或压缩包，并提交项目申请。
4. 管理员审批后，项目由 `launch_agents.sh` 创建，申请人获得该项目的 owner ACL。
5. 用户只能查看和控制自己获授权的项目。

### 管理员

管理员可以：

- 审批、拒绝、禁用或删除普通用户；`admin` 自身不能被删除。
- 审批或拒绝项目申请，也可直接创建项目。
- 查看全部项目、Secret Manager 元数据健康状态和最近审计记录。
- 对进行中的项目执行暂停、恢复和终止。

## 上传与创建项目

上传端点支持：

- 单文件：`.pdf`、`.md`
- 压缩包：`.zip`、`.tar`、`.tgz`、`.tar.gz`、`.tar.bz2`、`.tar.xz`

默认大小上限为 100 MB，可通过 `MAX_UPLOAD_SIZE` 调整。压缩包会在 `uploads/` 下安全解压并查找题目 PDF/Markdown；目录穿越归档会被拒绝。

管理员创建项目会直接调用：

```bash
./launch_agents.sh new [--no-start] [--consult] <base_name> "/abs/path/to/problem.pdf"
```

普通用户走项目申请/审批流程，不会绕过管理员批准。

## 题目归档

项目列表返回以下展示字段：

- `run_id`：当前运行目录名，与 `base_name` 一致。
- `problem_key`：优先根据项目内题目源文件计算 `sha256:<digest>`；没有可用源文件时退回 `project:<base_name>`。
- `problem_title`：从项目内 Markdown 标题提取，失败时使用项目名。
- `storage_scope`：`ongoing` 或 `complete`。
- `archived`：是否位于 `complete/`。

前端按 `problem_key` 分组并显示每道题的最新运行和历史运行。`ongoing/` 与 `complete/` 始终是存储和运行态真相；归档 UI 不是数据迁移。

## 人工选择：Web 与 CLI 并行

启用 `selection/config.json` 后，运行器会在 Step 3 前等待选择。可以在 Web 中提交，也可以在仓库根目录运行：

```bash
python3 scripts/selection_gate.py select-step3 ongoing/<base_name> \
  --primary m2 --aux m1 --reason "Prefer the verified primary stream"
```

调试时可加 `--no-resume`。CLI 路径是现役合同，不能被 Web 替代。

## API 概览

公开只读：

- `GET /api/showcase/papers`
- `GET /api/showcase/papers/{base_name}/pdf`

认证：

- `POST /api/auth/register`
- `POST /api/auth/login`
- `GET /api/auth/me`
- `POST /api/auth/logout`

项目与申请：

- `POST /api/upload/problem`
- `GET /api/projects`
- `POST /api/projects/new`（管理员）
- `GET|POST /api/project-requests`
- `POST /api/admin/project-requests/{request_id}/approve`
- `POST /api/admin/project-requests/{request_id}/reject`
- `POST /api/projects/{base_name}/action`

管理员：

- `GET /api/admin/users`
- `POST /api/admin/users/{username}/approve|reject|disable`
- `DELETE /api/admin/users/{username}`
- `GET /api/admin/ops/secrets`
- `GET /api/admin/audit-log`

其余项目详情、文件、日志、咨询、选择和模型配置接口均要求认证并执行项目 ACL 或管理员校验。

## 代码结构

```text
web/
├── backend/
│   ├── main.py           # FastAPI 主应用
│   ├── app.py            # 兼容启动器/重导出
│   ├── auth_store.py     # SQLite 用户、审批、ACL、审计
│   ├── project_api.py    # 上传、项目、咨询、选择、模型 API
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
  tests/test_web_frontend_runtime_helpers.py

bash -n scripts/load_secrets.sh scripts/setup_secret_manager.sh \
  web/backend/start.sh web/deploy.sh

cd web/frontend && npm run build
```

完整仓库 pytest 可能受历史测试重名和可选 Web/runtime 依赖影响；优先使用与变更合同对应的聚焦测试。

## 生产运维

生产服务、nginx、Secret Manager、部署、live smoke 和回滚步骤只以 [`docs/deployment/DEPLOYMENT.md`](docs/deployment/DEPLOYMENT.md) 为准。不要从旧“部署完成”报告复制命令或凭据。
