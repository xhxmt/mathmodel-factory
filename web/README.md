# Paper Factory Web Dashboard

实时监控 Paper Factory 任务进度的 Web 界面，支持登录认证、项目创建、人工介入和咨询回复。

## ✨ 新功能

### 🔐 登录认证系统
- JWT Token 认证，安全的会话管理
- 密码保护，支持环境变量配置
- Token 自动过期（默认 24 小时）

### 🚀 Web 界面创建项目
- 无需命令行，直接在网页上新建项目
- **支持上传题目文件**：PDF、Markdown 或**完整压缩包**（ZIP、TAR.GZ 等）
- **压缩包智能识别**：自动解压并查找题目文件，保留所有数据文件
- 指定项目名称和赛题 PDF 路径（或服务器路径）
- 可选：仅创建不自动启动、启用人工咨询模式

### 🧠 模型管理与逐步选模型
- **模型库**：在顶栏 <kbd>CPU</kbd> 图标打开「模型管理」，可添加 / 编辑 / 启停 / 删除可用模型。
  - Agentic 后端（`claude` / `codex` / `agy`）：可读写文件、跑求解器，适用于任意步骤。
  - API 后端（`openai` / `gemini` / `deepseek`）：HTTP 单轮调用（非 agentic），仅适用于评委/评审/评价步（7·11·13）。
  - 引入新模型（如 DeepSeek、Qwen）只需填 `backend=openai` + `base_url` + `key_env`，密钥放在仓库根 `.env`（如 `DEEPSEEK_API_KEY` / `DASHSCOPE_API_KEY`），此处只写变量名。
- **默认预设**：为**所有新建项目**设置每一步的默认主/备模型（写入 `_default`）。
- **逐项目逐步覆盖**：进入某项目工作台，在流水线里点选某一步，即可为该项目单独指定该步的主模型 + 备用模型。
- **Step 8.5 模型键**：`step_8_5` 对应 `Reviewer Entry Design`，默认建议使用 agentic 模型（Claude / Codex）。
- 留空＝沿用内置默认链，行为与改动前**完全一致**（无人值守 / 消融实验不受影响）。

## 功能特性

### 1. 实时项目监控
- 项目列表：显示所有 ongoing 和 complete 项目
- 状态跟踪：运行中、暂停、等待咨询、已完成等
- 进度可视化：步骤进度条（Step X/16）和百分比
- 工作台时间线会在 Step 8 和 Step 9 之间显示一个虚拟的 `8.5` 节点，用于提示“阅卷入口设计”是否完成
- 列表页可直接看到诊断徽章，例如“等待人工”“等待 8.5 门禁”“静默过久”
- 工作台顶部会显示诊断卡，包含主结论、最近关键事件和一键动作
- WebSocket 实时更新：无需刷新页面

### 2. 项目管理
- **创建项目**：通过 Web 界面创建新项目
- **控制操作**：暂停 (pause)、恢复 (resume)、终止 (kill)
- **详情查看**：checkpoint、日志、咨询请求

### 3. 人工介入
- 咨询请求展示：显示 gate、step、title、content
- 回答提交：粘贴 GPT Pro / Gemini Deep Think 的分析
- 自动写入 `human_review.md` 并恢复运行

## 快速开始

### 步骤 1：配置环境变量

首次使用需要配置管理员密码：

```bash
cd web
cp .env.example .env
```

编辑 `.env` 文件，修改密码：
```bash
ADMIN_PASSWORD=your_secure_password
```

### 步骤 2：一键启动

```bash
cd web
./start_dashboard.sh
```

这会自动：
1. 安装后端依赖（Python venv + pip）
2. 安装前端依赖（npm install）
3. 启动后端服务器（端口 8000）
4. 启动前端开发服务器（端口 5173）

### 步骤 3：登录系统

打开浏览器访问：**http://localhost:5173**

默认登录凭据：
- 用户名：`admin`
- 密码：`admin123`（或你在 `.env` 中设置的密码）

## 使用指南

### 新建项目

1. 登录后，点击右上角 **"➕ 新建项目"** 按钮
2. 填写项目信息：
   - **项目名称**：例如 `cumcm2024_a`（只能包含字母、数字、下划线和连字符）
   - **题目文件**：支持两种方式
     - **上传文件**：拖拽或点击上传
       - 单个题目文件：PDF 或 Markdown
       - **完整压缩包**：ZIP、TAR.GZ、TAR.BZ2、TAR.XZ（包含题目+数据+附件）
       - 压缩包会自动解压并智能识别题目文件
     - **服务器路径**：输入服务器上文件的绝对路径（如 `/home/user/problems/2024_A.pdf`）
   - **选项**：
     - ☑️ 仅创建项目，不自动开始执行
     - ☑️ 启用人工咨询模式
3. 点击 **"创建项目"**
4. 项目创建成功后会自动出现在列表中

**💡 压缩包上传提示**：
- 竞赛题目通常包含多个附件，推荐使用压缩包一次性上传
- 系统会自动识别名称包含"题目"、"problem"、"question"的文件
- 所有数据文件会保留在解压目录，供后续步骤使用
- 详细说明见 [压缩包上传快速开始](ARCHIVE_UPLOAD_QUICKSTART.md)

### 监控项目

Dashboard 显示所有项目的实时状态：
- **运行中**（蓝色）：正在执行，有活跃的 PID
- **等待咨询**（黄色）：等待人工输入
- **暂停**（灰色）：用户手动暂停
- **已完成**（绿色）：所有 16 步完成
- **已终止**（红色）：用户终止

### 处理咨询请求

当项目显示 "等待咨询" 状态：

1. 点击项目卡片的 **"查看详情"**
2. 切换到 **"人工咨询"** 标签页
3. 查看咨询请求的具体内容
4. 将问题复制到 GPT Pro / Gemini Deep Think
5. 将模型的回答粘贴到文本框
6. 点击 **"提交并恢复运行"**

系统会自动写入 `human_review.md` 并标记 `STATUS: READY`。

### 控制项目

在项目卡片或详情页面：
- **暂停**：暂停项目执行
- **恢复**：恢复暂停的项目
- **终止**：完全停止项目

### 诊断能力

- 列表页会展示项目诊断徽章，快速说明当前阻塞类型或风险状态。
- 工作台会展示诊断卡，汇总 `reason_code`、最近事件和可执行动作。
- 诊断动作不会引入新的控制面入口，统一复用已有日志、产物和项目控制能力。
- 新增接口：`GET /api/projects/{base_name}/diagnostics`

返回示例：

```json
{
  "source": "runner",
  "status": {
    "reason_code": "AWAITING_STEP8_5",
    "reason_summary": "Step 8.5 未通过，等待补足 reviewer entry 材料"
  },
  "actions": [
    { "id": "open_entry_gate" },
    { "id": "refresh_status" }
  ]
}
```

## API 端点

### 认证

- `POST /api/auth/login` - 登录
  ```json
  {"username": "admin", "password": "admin123"}
  ```
- `GET /api/auth/me` - 获取当前用户信息
- `POST /api/auth/logout` - 登出

### 项目管理（需要认证）

所有端点需要在 Header 中携带 JWT Token：
```
Authorization: Bearer <your_token>
```

- `GET /api/projects` - 获取所有项目列表
- `POST /api/projects/new` - 创建新项目
- `POST /api/upload/problem` - 上传题目文件或压缩包（返回文件路径）
  - 支持格式：`.pdf`, `.md`, `.zip`, `.tar.gz`, `.tar.bz2`, `.tar.xz`
  - 压缩包会自动解压并识别题目文件
  - 返回示例：
    ```json
    {
      "status": "ok",
      "file_path": "/path/to/uploads/20260621_123456_archive/题目.pdf",
      "filename": "题目.pdf",
      "extracted_dir": "/path/to/uploads/20260621_123456_archive"
    }
    ```
- `GET /api/projects/{base_name}/status` - 获取项目状态
- `GET /api/projects/{base_name}/diagnostics` - 获取项目诊断摘要、最近事件和动作建议
- `GET /api/projects/{base_name}/checkpoint` - 获取 checkpoint 内容
- `GET /api/projects/{base_name}/logs?lines=100` - 获取日志
- `POST /api/projects/{base_name}/action` - 执行操作（pause/resume/kill）

### 咨询管理

- `GET /api/projects/{base_name}/consultation` - 获取咨询请求
- `POST /api/projects/{base_name}/consultation/answer` - 提交咨询回答

### 模型管理（需要认证）

- `GET /api/models` - 模型库 + 逐步分配配置（注册表 / config / agentic 后端列表）
- `PUT /api/models/registry` - 整体保存模型库 `{ "models": [...] }`
- `PUT /api/models/config` - 保存某作用域的逐步分配 `{ "scope": "<base>|_default", "steps": { "step_13": {"primary": "<id>", "fallback": "<id>"} } }`

> 配置文件：`web/model_registry.json`（模型库）、`web/model_config.json`（逐步分配，`_default` 为全局预设、`<base>` 为单项目覆盖）。两者均被 `.gitignore`，由后端首次访问时按内置默认播种；`run_paper.sh` 直接读取它们（见其 “Model registry & per-step model dispatch” 段）。

### WebSocket

- `WS /ws` - 实时状态更新
  - `status_update`: 全量项目状态（每 2 秒）
  - `project_updated`: 单个项目状态变化
  - `project_created`: 新项目创建
  - `consultation_answered`: 咨询已回答
  - `project_action`: 项目操作执行

## 安全配置

### 修改管理员密码

编辑 `web/.env` 文件：
```bash
ADMIN_PASSWORD=your_new_secure_password
```

重启服务后生效。

### JWT Secret

系统会自动生成随机的 JWT Secret。固定 Secret（多实例部署）：
```bash
JWT_SECRET=your-secret-key-here
```

### Token 有效期

默认 24 小时。修改 `web/backend/app.py`：
```python
JWT_EXPIRATION_HOURS = 24
```

### 添加用户

编辑 `web/backend/app.py` 中的 `USERS_DB`：
```python
USERS_DB = {
    "admin": {
        "password_hash": hashlib.sha256("admin123".encode()).hexdigest(),
        "username": "admin",
        "role": "admin"
    },
    "researcher1": {
        "password_hash": hashlib.sha256("password123".encode()).hexdigest(),
        "username": "researcher1",
        "role": "user"
    }
}
```

## 技术架构

```
web/
├── backend/
│   ├── app.py              # FastAPI 后端（API + WebSocket + 认证）
│   └── start.sh
├── frontend/
│   ├── src/
│   │   ├── App.vue                    # 主应用
│   │   └── components/
│   │       ├── LoginForm.vue          # 登录表单
│   │       ├── NewProjectModal.vue    # 新建项目对话框
│   │       ├── ProjectCard.vue        # 项目卡片
│   │       └── ProjectDetailModal.vue # 项目详情
│   ├── package.json
│   └── start.sh
├── .env.example           # 环境变量模板
├── requirements.txt       # Python 依赖
└── start_dashboard.sh     # 一键启动脚本
```

### 技术栈

**后端：**
- FastAPI - Web 框架
- Pydantic - 数据验证
- PyJWT - JWT Token 处理
- uvicorn - ASGI 服务器

**前端：**
- Vue 3 - JavaScript 框架
- Axios - HTTP 客户端
- WebSocket - 实时通信

## 常见问题

### Q: 登录后立即被登出？
A: 检查系统时间是否正确。JWT Token 使用 UTC 时间戳验证。

### Q: 无法创建项目？
A: 确保：
1. 题目文件路径正确且可访问
2. `launch_agents.sh` 有执行权限
3. 项目名称不与现有项目冲突

### Q: WebSocket 连接失败？
A: 如果使用反向代理（如 nginx），配置 WebSocket 支持：
```nginx
location /ws {
    proxy_pass http://localhost:8000;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
}
```

### Q: 后端启动失败？
A: 检查端口占用：
```bash
lsof -i :8000
```

## 分别启动（开发用）

如果需要单独控制前后端：

```bash
# 后端（终端 1）
cd web/backend
./start.sh

# 前端（终端 2）
cd web/frontend
./start.sh
```

## 生产部署

### 后端

使用 gunicorn + uvicorn workers：
```bash
pip install gunicorn
gunicorn app:app -w 4 -k uvicorn.workers.UvicornWorker --bind 0.0.0.0:8000
```

### 前端

构建静态文件：
```bash
cd web/frontend
npm run build
# 将 dist/ 目录部署到 nginx/caddy
```

### 反向代理（nginx）

```nginx
server {
    listen 80;
    server_name your-domain.com;

    location / {
        root /path/to/web/frontend/dist;
        try_files $uri $uri/ /index.html;
    }

    location /api {
        proxy_pass http://127.0.0.1:8000;
    }

    location /ws {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }
}
```

### 进程管理（systemd）

创建 `/etc/systemd/system/paper-factory-api.service`：
```ini
[Unit]
Description=Paper Factory API
After=network.target

[Service]
Type=simple
User=tfisher
WorkingDirectory=/home/tfisher/paper_factory/web/backend
Environment="PATH=/home/tfisher/paper_factory/web/venv/bin"
ExecStart=/home/tfisher/paper_factory/web/venv/bin/gunicorn app:app -w 4 -k uvicorn.workers.UvicornWorker --bind 0.0.0.0:8000
Restart=always

[Install]
WantedBy=multi-user.target
```

启动服务：
```bash
sudo systemctl enable paper-factory-api
sudo systemctl start paper-factory-api
```

## 开发

### 前端开发

Vite 支持热更新（HMR），编辑 `.vue` 文件后浏览器自动刷新。

### 后端开发

FastAPI 支持自动重载：
```bash
cd web/backend
uvicorn app:app --reload --host 0.0.0.0 --port 8000
```

## 许可证

与 Paper Factory 主项目保持一致。
