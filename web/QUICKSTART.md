# Web Dashboard 快速开始

这是本地开发/操作的最短路径。完整功能和权限说明见 [`README.md`](README.md)，生产部署见 [`docs/deployment/DEPLOYMENT.md`](docs/deployment/DEPLOYMENT.md)。

## 1. 准备依赖和 Secret Manager

需要 Python 3、Node.js `^20.19.0` 或 `>=22.12.0`、npm、`gcloud` CLI，以及当前 GCP 项目的 Secret Manager 访问权限。

```bash
gcloud auth login
gcloud config set project <GCP_PROJECT_ID>
uv sync --extra web --extra models --locked
(cd web/frontend && npm ci)
```

不要在终端输出或复制 secret 值。只验证账号、项目、secret 元数据和访问是否成功。

## 2. 配置非敏感环境变量

```bash
cd /home/tfisher/paper_factory
cp web/.env.example web/.env
```

编辑 `web/.env`，至少填写 `GCP_PROJECT_ID`。`web/.env` 不应包含 `JWT_SECRET`、`ADMIN_PASSWORD` 或 API key；这些值由 `scripts/load_secrets.sh` 从 Secret Manager 加载。

Phase 6 验证快照默认关闭，普通本地启动不需要任何 Phase 6 配置，仍使用
完整的 v1 Dashboard。仅在已经有受审查的独立 Phase 6 SQLite 候选时才同时配置：

```dotenv
# web/.env（后端；非敏感）
PHASE6_SNAPSHOT_ENABLED=true
PHASE6_SNAPSHOT_DB_FILE=/home/tfisher/paper_factory/run_state/phase6_snapshot_shadow.db
```

并在 `web/frontend/.env.local` 中写入：

```dotenv
VITE_PHASE6_FULL_SHADOW_ENABLED=true
```

后端 flag 缺省为 false；前端只接受精确的小写字符串 `true`。自定义数据库
路径必须是绝对路径，父目录必须预先存在、没有符号链接，数据库必须由 Phase 6
受审查 producer/harness 创建，不能拿普通或空 SQLite 文件代替。前后端 flag
需要一致启用；详情见 [`README.md`](README.md#phase-6-验证快照默认关闭)。

## 3. 启动 Dashboard

```bash
cd /home/tfisher/paper_factory/web
./start_dashboard.sh
```

启动器会：

1. 通过 `web/backend/start.sh` 加载 Secret Manager 配置并启动 FastAPI；
2. 等待 <http://127.0.0.1:8000/> 就绪；
3. 启动 Vite 开发服务器。

启动器只使用已准备的根 `.venv` 和 `node_modules`，不会在运行时创建虚拟环境或安装依赖。

浏览器打开 <http://localhost:5173>。

启用 Phase 6 时，登录后只会在用户原本有项目 ACL 的工作区出现“验证快照”页。
它是非权威只读页，不会执行 action、写项目数据库或改变运行状态。若要回到默认
路径，将 `PHASE6_SNAPSHOT_ENABLED=false`，移除或关闭前端 flag，随后重启
Dashboard；独立 Phase 6 数据库会保留。

## 4. 登录或注册

- 管理员账号名为 `admin`，密码来自 Secret Manager；不存在默认密码。
- 普通用户在页面注册后状态为 `pending`，需管理员审批才能登录。
- 未登录访客只能浏览管理员授予默认访客的公开完成论文；注册用户登录后还可浏览管理员单独授予自己的论文。

## 5. 创建项目

- 管理员：在“新建项目”中上传题目或填写服务器路径，直接创建。
- 普通用户：提交项目申请；管理员批准后系统创建项目并授予申请人 ACL。
- 支持 PDF、Markdown、ZIP/TAR 系列压缩包，默认最大 100 MB。

项目创建后，Dashboard 会显示运行状态、诊断、日志、文件和人工介入入口。同一题目的重复运行会聚合到一个题目归档中。

## 常见故障

### 后端拒绝启动

检查：

```bash
cd /home/tfisher/paper_factory
source scripts/load_secrets.sh
```

该命令只应报告加载成功或明确错误，不应打印任何值。确认 `GCP_PROJECT_ID`、gcloud 登录状态、IAM 权限，以及必需 secret 是否存在。

### 登录失败

- 确认用户已被审批且状态为 `active`。
- 管理员密码轮换后需重启后端，使 SQLite 中的管理员哈希与 Secret Manager 同步。
- 不要通过 `cat`, `grep`, shell tracing 或日志查看密码。

### 项目不可见

- 管理员可见全部项目。
- 普通用户只看到 `project_acl` 中已授权的项目；请检查项目申请是否已批准。

### Phase 6 标签或快照不可用

- 没有标签：确认启动 Vite 前 `web/frontend/.env.local` 中的值精确为 `true`。
- 显示“暂不提供快照”：确认后端 flag 已启用、用户已有项目 ACL，并且独立
  SQLite 中存在该项目的 COMPLETE、contract-pin-bound current verified
  snapshot；PARTIAL source 只保留审计证据，不能进入 Web ready 状态。
- 后端拒绝启动并报告路径错误：`PHASE6_SNAPSHOT_DB_FILE` 必须是绝对路径。
- API 返回 stale：刷新页面；客户端会自动进行至多一次无 revision 重读，不会
  无限重试。
- API 返回 unavailable/inconsistent：保持失败关闭，检查后端日志中的错误类型；
  公共响应不会显示路径、SQL 或内部异常。不要用新空库覆盖原库来绕过校验。

### Step 3 等待选择

除 Web 外，也可在仓库根目录使用 CLI：

```bash
python3 scripts/selection_gate.py select-step3 ongoing/<base_name> \
  --primary m2 --aux m1 --reason "Selected after reviewing verified streams"
```

Step 16 前用相同 Web 选择面板确认 `content_freeze`，或运行：

```bash
python3 scripts/selection_gate.py approve-content-freeze ongoing/<base_name> \
  --reason "Final human review complete"
```
