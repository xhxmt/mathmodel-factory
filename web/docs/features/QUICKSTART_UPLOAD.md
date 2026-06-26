# 🚀 快速启动：Web 文件上传功能

## 启动服务

### 1. 启动后端
```bash
cd /home/tfisher/paper_factory/web
python3 backend/app.py
```

后端将运行在 `http://0.0.0.0:8000`

### 2. 启动前端（开发模式）
```bash
cd /home/tfisher/paper_factory/web/frontend
npm run dev
```

前端将运行在 `http://localhost:5173`

### 3. 访问应用
打开浏览器访问：`http://localhost:5173`

默认登录：
- 用户名：`admin`
- 密码：`admin123`

## 测试上传功能

### 准备测试文件
```bash
# 创建一个测试 PDF（如果有真实题目文件更好）
echo "Test PDF content" > /tmp/test_problem.pdf

# 或创建一个 Markdown 测试文件
cat > /tmp/test_problem.md << 'EOF'
# 2024 数学建模竞赛 A 题

## 问题描述
这是一个测试问题...

## 数据说明
- data1.csv
- data2.csv

## 要求
1. 建立数学模型
2. 进行数值求解
3. 撰写论文
EOF
```

### 测试步骤

1. **登录 Web Dashboard**
   - 访问 http://localhost:5173
   - 输入用户名/密码
   - 点击登录

2. **创建项目（上传文件）**
   - 点击右上角 **"➕ 新建项目"** 按钮
   - 输入项目名称：`test_upload_001`
   - 确保选择 **"📤 上传文件"** 标签
   - **方式A - 点击上传**：
     - 点击上传区域
     - 选择 `/tmp/test_problem.pdf`
   - **方式B - 拖拽上传**：
     - 从文件管理器拖拽 `test_problem.pdf` 到上传区域
   - 观察：
     - ✅ 文件卡片显示（文件名、大小）
     - ✅ 进度条从 0% → 100%
   - 勾选 **"启用人工咨询模式"**（可选）
   - 点击 **"创建项目"** 按钮

3. **验证上传结果**
   ```bash
   # 查看上传的文件
   ls -lh /home/tfisher/paper_factory/uploads/
   
   # 应该看到类似：
   # 20240615_143022_test_problem.pdf
   
   # 查看项目是否创建
   ls -lh /home/tfisher/paper_factory/ongoing/test_upload_001/
   ```

4. **测试指定路径方式（对比）**
   - 点击 **"➕ 新建项目"**
   - 输入项目名称：`test_path_001`
   - 选择 **"📁 指定路径"** 标签
   - 输入：`/tmp/test_problem.md`
   - 点击 **"创建项目"**

## 功能检查清单

### 基本上传
- [ ] 点击上传 PDF 文件 - 成功
- [ ] 拖拽上传 PDF 文件 - 成功
- [ ] 上传 Markdown 文件 - 成功
- [ ] 文件信息显示正确（名称、大小、图标）
- [ ] 进度条正常工作（0% → 100%）
- [ ] 上传完成后能创建项目

### UI 交互
- [ ] 切换到"指定路径"模式 - 表单正确显示
- [ ] 切换回"上传文件"模式 - 恢复上传界面
- [ ] 点击 ✕ 删除文件 - 文件卡片消失
- [ ] 拖拽时上传区域高亮 - 蓝色边框+背景
- [ ] 悬停上传区域 - 边框变蓝

### 错误处理
- [ ] 上传 .txt 文件 → 显示错误："仅支持 PDF 或 Markdown 文件"
- [ ] 上传超大文件（如果有）→ 显示错误："文件过大"
- [ ] 未选择文件直接创建 → 显示错误："请选择题目文件"
- [ ] 指定不存在的路径 → 后端返回错误

### 不同文件类型
- [ ] `.pdf` 文件 → 显示 📕 图标
- [ ] `.PDF` 文件 → 显示 📕 图标
- [ ] `.md` 文件 → 显示 📝 图标
- [ ] `.MD` 文件 → 显示 📝 图标

## 生产部署

### 后端（使用 systemd 服务）

创建服务文件 `/etc/systemd/system/paper-factory-web.service`：

```ini
[Unit]
Description=Paper Factory Web Dashboard
After=network.target

[Service]
Type=simple
User=tfisher
WorkingDirectory=/home/tfisher/paper_factory/web
ExecStart=/usr/bin/python3 /home/tfisher/paper_factory/web/backend/app.py
Restart=on-failure
RestartSec=5s

[Install]
WantedBy=multi-user.target
```

启动服务：
```bash
sudo systemctl daemon-reload
sudo systemctl enable paper-factory-web
sudo systemctl start paper-factory-web
sudo systemctl status paper-factory-web
```

### 前端（构建生产版本）

```bash
cd /home/tfisher/paper_factory/web/frontend
npm run build
```

构建产物在 `dist/` 目录，使用 Nginx 部署：

```nginx
server {
    listen 80;
    server_name your-domain.com;

    # 静态文件
    location / {
        root /home/tfisher/paper_factory/web/frontend/dist;
        try_files $uri $uri/ /index.html;
    }

    # API 代理
    location /api {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }

    # WebSocket 代理
    location /ws {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }

    # 上传大小限制
    client_max_body_size 100M;
}
```

## 故障排除

### 问题 1：上传按钮无反应
**检查**：浏览器控制台是否有错误
```bash
# 检查后端是否运行
curl http://localhost:8000/
# 应返回：{"status":"Paper Factory Dashboard API","version":"1.0.0"}
```

### 问题 2：上传卡在某个百分比
**检查**：后端日志
```bash
# 查看后端日志
tail -f /home/tfisher/paper_factory/web/backend.log

# 或直接查看进程输出
ps aux | grep "python.*app.py"
```

### 问题 3：上传后文件找不到
**检查**：目录权限
```bash
ls -ld /home/tfisher/paper_factory/uploads/
# 应该是 755 或更宽松

# 如果权限有问题
chmod 755 /home/tfisher/paper_factory/uploads/
```

### 问题 4：CORS 错误
**解决**：在 `backend/app.py` 中添加你的域名到 `allow_origins` 列表

### 问题 5：上传后项目创建失败
**检查**：
```bash
# 查看 launch_agents.sh 是否可执行
ls -l /home/tfisher/paper_factory/launch_agents.sh

# 查看上传的文件路径是否正确
cat /home/tfisher/paper_factory/ongoing/test_upload_001/problem/source.md
```

## 日志位置

- **前端开发日志**：浏览器控制台（F12）
- **后端日志**：终端输出或 `web/backend.log`（如果配置）
- **项目运行日志**：`logs/` 目录
- **项目具体日志**：`ongoing/<project>/logs/`

## 性能优化

### 大文件上传优化

如果需要支持更大的文件（如 500 MB），需要调整：

1. **后端限制**：
```python
# backend/app.py
MAX_UPLOAD_SIZE = 500 * 1024 * 1024  # 500 MB
```

2. **Nginx 限制**：
```nginx
client_max_body_size 500M;
```

3. **前端超时**：
```javascript
// axios 配置
axios.post('/api/upload/problem', formData, {
  timeout: 300000  // 5 分钟
})
```

## 监控

### 查看上传统计
```bash
# 查看上传文件数量
ls /home/tfisher/paper_factory/uploads/ | wc -l

# 查看总大小
du -sh /home/tfisher/paper_factory/uploads/

# 查看最近上传的文件
ls -lt /home/tfisher/paper_factory/uploads/ | head -10
```

### 清理旧文件
```bash
# 查看 30 天前的文件
find /home/tfisher/paper_factory/uploads/ -type f -mtime +30 -ls

# 删除 30 天前的文件
find /home/tfisher/paper_factory/uploads/ -type f -mtime +30 -delete
```

---

**功能已就绪！开始享受无缝的文件上传体验吧！** 🎉
