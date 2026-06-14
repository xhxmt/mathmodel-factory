# Paper Factory Web Dashboard

实时监控 Paper Factory 任务进度的 Web 界面，支持人工介入和咨询回复。

## 功能特性

### 1. 实时项目监控
- **项目列表**：显示所有 ongoing 和 complete 项目
- **状态跟踪**：运行中、暂停、等待咨询、已完成等状态
- **进度可视化**：步骤进度条（Step X/16）和百分比
- **WebSocket 实时更新**：无需刷新页面，自动同步最新状态

### 2. 项目管理
- **控制操作**：暂停 (pause)、恢复 (resume)、终止 (kill) 项目
- **详情查看**：
  - 概览：checkpoint.md 内容、项目元数据
  - 日志：最近的执行日志（可刷新）
  - 人工咨询：pending consultation 请求列表

### 3. 人工介入机制
- **咨询请求展示**：显示 gate、step、title、content
- **回答提交**：
  - 粘贴 GPT Pro / Gemini Deep Think 的分析结果
  - 自动写入 `human_review.md` 并标记 `STATUS: READY`
  - 提交后自动触发项目恢复（如果之前在等待）

## 技术架构

```
web/
├── backend/              # FastAPI 后端
│   ├── app.py           # 主应用（API + WebSocket）
│   ├── requirements.txt # Python 依赖
│   └── start.sh         # 后端启动脚本
├── frontend/            # Vue 3 前端
│   ├── src/
│   │   ├── App.vue                           # 主应用
│   │   └── components/
│   │       ├── ProjectCard.vue               # 项目卡片
│   │       └── ProjectDetailModal.vue        # 详情弹窗
│   ├── index.html       # 入口 HTML
│   ├── vite.config.js   # Vite 配置
│   ├── package.json     # Node 依赖
│   └── start.sh         # 前端启动脚本
└── start_dashboard.sh   # 一键启动脚本
```

## 快速开始

### 一键启动（推荐）

```bash
cd web
./start_dashboard.sh
```

然后访问：**http://localhost:5173**

脚本会自动：
1. 安装后端依赖（Python venv + pip）
2. 安装前端依赖（npm install）
3. 启动后端服务器（端口 8000）
4. 启动前端开发服务器（端口 5173）

按 `Ctrl+C` 停止所有服务。

### 分别启动

如果需要单独启动：

```bash
# 后端（终端 1）
cd web/backend
./start.sh

# 前端（终端 2）
cd web/frontend
./start.sh
```

## API 端点

### 项目管理

- `GET /api/projects` - 获取所有项目列表
- `GET /api/projects/{base_name}/status` - 获取单个项目状态
- `GET /api/projects/{base_name}/checkpoint` - 获取 checkpoint.md 内容
- `GET /api/projects/{base_name}/logs?lines=100` - 获取最近日志
- `POST /api/projects/{base_name}/action` - 执行操作（pause/resume/kill）

### 咨询管理

- `GET /api/projects/{base_name}/consultation` - 获取待处理咨询请求
- `POST /api/projects/{base_name}/consultation/answer` - 提交咨询回答
  ```json
  {
    "answer": "## 建模方案分析\n\n经过 GPT Pro 深度思考..."
  }
  ```

### WebSocket

- `WS /ws` - 实时状态推送
  - 消息类型：
    - `status_update`: 全量项目状态更新（每 2 秒）
    - `project_updated`: 单个项目状态变化
    - `consultation_answered`: 咨询已回答
    - `project_action`: 项目操作已执行

## 使用场景

### 场景 1：监控批量实验

```bash
# 启动多个 ablation 实验
./launch_agents.sh new cumcm2024b_no_methodlib_rep1 /path/to/problem.pdf
./launch_agents.sh new cumcm2024b_no_judge_rep1 /path/to/problem.pdf

# 打开 Dashboard 监控
cd web && ./start_dashboard.sh
```

在浏览器中实时查看：
- 每个实验的当前步骤
- 进度条和完成百分比
- 是否有卡住或失败的任务

### 场景 2：处理咨询请求

当项目进入 `consultation` 状态（黄色高亮 "等待咨询"）：

1. 点击项目卡片的 **"查看详情"**
2. 切换到 **"人工咨询"** 标签页
3. 查看咨询请求的具体内容（例如 preflight、step4 gate）
4. 将问题复制到 GPT Pro / Gemini Deep Think
5. 将模型的回答粘贴到文本框
6. 点击 **"提交并恢复运行"**

系统会自动：
- 写入 `human_review.md` 并标记 `STATUS: READY`
- 如果项目在 `awaiting_consultation` 状态，后续 `resume` 会自动触发

### 场景 3：查看运行日志

点击项目卡片 → 详情弹窗 → "日志" 标签页：
- 显示最近 100 行日志
- 点击 "刷新" 按钮获取最新日志
- 快速诊断 solver 错误、step 失败等问题

## 状态说明

| 状态                     | 颜色 | 说明                                   |
|--------------------------|------|----------------------------------------|
| `running`                | 蓝色 | 正在运行，有活跃的 PID                 |
| `awaiting_consultation`  | 黄色 | 等待人工咨询回答，runner 已退出        |
| `paused`                 | 灰色 | 用户手动暂停（`.paused` 标记存在）     |
| `completed`              | 绿色 | 所有 16 步已完成，项目在 `complete/`   |
| `killed`                 | 红色 | 用户终止（`.killed` 标记存在）         |

## 依赖要求

### 后端
- Python 3.8+
- FastAPI 0.115+
- uvicorn
- websockets

### 前端
- Node.js 18+
- Vue 3
- Vite 5
- axios

## 开发说明

### 修改后端代码

编辑 `backend/app.py` 后，重启后端服务即可：

```bash
cd web/backend
./start.sh
```

### 修改前端代码

Vite 支持热更新（HMR），编辑 `.vue` 文件后浏览器自动刷新。

### 添加新功能

例如，添加 "一键清理完成项目" 功能：

1. 后端：在 `app.py` 添加端点
   ```python
   @app.post("/api/cleanup/completed")
   async def cleanup_completed():
       # 实现逻辑
       pass
   ```

2. 前端：在 `App.vue` 添加按钮和调用
   ```vue
   <button @click="cleanupCompleted">清理已完成项目</button>
   ```

## 故障排查

### 后端启动失败

检查端口占用：
```bash
lsof -i :8000
```

### 前端无法连接后端

1. 确认后端已启动：`curl http://127.0.0.1:8000/`
2. 检查 CORS 配置（`app.py` 中已配置 localhost:5173）

### WebSocket 连接失败

浏览器控制台应该显示连接状态，如果失败：
1. 检查后端是否运行
2. 检查防火墙设置
3. 查看后端日志

## 生产部署

当前为本地开发模式。生产部署建议：

1. **后端**：使用 gunicorn + uvicorn workers
   ```bash
   gunicorn app:app -w 4 -k uvicorn.workers.UvicornWorker --bind 0.0.0.0:8000
   ```

2. **前端**：构建静态文件
   ```bash
   npm run build
   # 将 dist/ 目录部署到 nginx/caddy
   ```

3. **反向代理**：使用 nginx 统一入口
   ```nginx
   location /api {
       proxy_pass http://127.0.0.1:8000;
   }
   location /ws {
       proxy_pass http://127.0.0.1:8000;
       proxy_http_version 1.1;
       proxy_set_header Upgrade $http_upgrade;
       proxy_set_header Connection "upgrade";
   }
   ```

4. **进程管理**：使用 systemd 或 supervisor 管理后端进程

## 许可

与 Paper Factory 主项目相同。
