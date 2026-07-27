# Web Dashboard 快速开始

这是本地开发/操作的最短路径。完整功能和权限说明见 [`README.md`](README.md)，生产部署见 [`docs/deployment/DEPLOYMENT.md`](docs/deployment/DEPLOYMENT.md)。

## 1. 准备依赖和 Secret Manager

需要 Python 3、Node.js/npm、`gcloud` CLI，以及当前 GCP 项目的 Secret Manager 访问权限。

```bash
gcloud auth login
gcloud config set project <GCP_PROJECT_ID>
```

不要在终端输出或复制 secret 值。只验证账号、项目、secret 元数据和访问是否成功。

## 2. 配置非敏感环境变量

```bash
cd /home/tfisher/paper_factory
cp web/.env.example web/.env
```

编辑 `web/.env`，至少填写 `GCP_PROJECT_ID`。`web/.env` 不应包含 `JWT_SECRET`、`ADMIN_PASSWORD` 或 API key；这些值由 `scripts/load_secrets.sh` 从 Secret Manager 加载。

## 3. 启动 Dashboard

```bash
cd /home/tfisher/paper_factory/web
./start_dashboard.sh
```

启动器会：

1. 通过 `web/backend/start.sh` 加载 Secret Manager 配置并启动 FastAPI；
2. 等待 <http://127.0.0.1:8000/> 就绪；
3. 启动 Vite 开发服务器。

浏览器打开 <http://localhost:5173>。

## 4. 登录或注册

- 管理员账号名为 `admin`，密码来自 Secret Manager；不存在默认密码。
- 普通用户在页面注册后状态为 `pending`，需管理员审批才能登录。
- 未登录访客只能浏览 `SHOWCASE_PROJECTS` 中的公开完成论文。

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

### Step 3 等待选择

除 Web 外，也可在仓库根目录使用 CLI：

```bash
python3 scripts/selection_gate.py select-step3 ongoing/<base_name> \
  --primary m2 --aux m1 --reason "Selected after reviewing verified streams"
```
