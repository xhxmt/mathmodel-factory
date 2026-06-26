# 部署到 tfisher.de 服务器

## 前端部署

### 1. 本地构建完成
前端已成功构建，输出在：
- 位置: `/home/tfisher/paper_factory/web/frontend/dist/`
- 打包文件: `/home/tfisher/paper_factory/web/frontend/frontend-dist.tar.gz` (1007 KB)

### 2. 部署步骤

#### 方法 1: 手动上传（推荐）

```bash
# 从本地上传到服务器
scp -P <SSH_PORT> frontend-dist.tar.gz tfisher@tfisher.de:/tmp/

# SSH 登录服务器
ssh -p <SSH_PORT> tfisher@tfisher.de

# 在服务器上执行
cd /var/www/paper-factory
sudo tar xzf /tmp/frontend-dist.tar.gz
sudo chown -R www-data:www-data /var/www/paper-factory
rm /tmp/frontend-dist.tar.gz
```

#### 方法 2: 使用 rsync（需要安装 rsync）

```bash
rsync -avz -e "ssh -p <SSH_PORT>" dist/ tfisher@tfisher.de:/var/www/paper-factory/
```

#### 方法 3: Git 部署

```bash
# 在服务器上
cd /path/to/paper_factory
git pull origin modeling-factory
cd web/frontend
npm run build
sudo cp -r dist/* /var/www/paper-factory/
sudo chown -R www-data:www-data /var/www/paper-factory
```

### 3. 验证部署

访问: https://tfisher.de/paper-factory/

检查：
- ✅ 咨询面板显示 AI 工具快捷入口（ChatGPT、Gemini、Claude）
- ✅ 计算密集型步骤显示云端加速弹窗
- ✅ 浏览器控制台无错误

## 后端部署

### 1. 更新后端代码

```bash
# 在服务器上
cd /path/to/paper_factory
git pull origin modeling-factory

# 重启后端服务
sudo systemctl restart paper-factory-backend
# 或者如果使用 pm2
pm2 restart paper-factory-backend
```

### 2. 后端新增端点

确保以下端点可访问：
- `GET /api/cloud/status` - 检查 Cloud Run 状态
- `GET /api/cloud/config` - 查询配置
- `POST /api/projects/{base}/cloud/enable` - 启用加速
- `POST /api/projects/{base}/cloud/disable` - 禁用加速

### 3. 环境配置

确保 `web/.env` 包含 GCP 配置：
```bash
GCP_PROJECT_ID=level-night-476302-k0
GCP_REGION=europe-west4
GCP_SOLVER_SERVICE=solver-api
JWT_SECRET=<your-secret>
ADMIN_PASSWORD=<your-password>
```

## 功能测试清单

### 人工咨询入口测试
- [ ] 创建带 `--consult` 的项目
- [ ] 等待项目进入 consultation_pending 状态
- [ ] 验证咨询面板显示三个 AI 工具卡片
- [ ] 点击卡片能在新标签页打开对应工具
- [ ] 提交咨询结论后项目恢复运行

### 云端加速弹窗测试
- [ ] 创建常规项目（不带 --consult）
- [ ] 等待项目进入 Step 5 或 Step 6
- [ ] 5 秒后自动弹出云端加速对话框
- [ ] 对话框显示预估效果和服务状态
- [ ] 点击"启用云端加速"创建 `.env.cloud` 文件
- [ ] 后续长任务自动路由到 Cloud Run

### API 端点测试

```bash
# 获取 token
TOKEN=$(curl -X POST https://tfisher.de/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"your-password"}' \
  | jq -r .access_token)

# 测试 Cloud Run 状态
curl -H "Authorization: Bearer $TOKEN" \
  https://tfisher.de/api/cloud/status

# 测试启用云端加速
curl -X POST \
  -H "Authorization: Bearer $TOKEN" \
  https://tfisher.de/api/projects/test_project/cloud/enable
```

## 回滚方案

如果部署出现问题，可以快速回滚：

```bash
# 在服务器上
cd /var/www/paper-factory
sudo git checkout HEAD~1 -- .
sudo chown -R www-data:www-data .

# 重启后端
sudo systemctl restart paper-factory-backend
```

## Nginx 配置（如需更新）

如果需要调整 Nginx 配置：

```nginx
location /api/ {
    proxy_pass http://127.0.0.1:8000/api/;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_read_timeout 300s;
    proxy_connect_timeout 75s;
}

location / {
    root /var/www/paper-factory;
    try_files $uri $uri/ /index.html;
    add_header Cache-Control "no-cache";
}
```

重载配置：
```bash
sudo nginx -t
sudo systemctl reload nginx
```

## 监控和日志

### 前端
- 浏览器控制台: 检查 JavaScript 错误
- Network 面板: 检查 API 请求状态

### 后端
```bash
# 查看后端日志
sudo journalctl -u paper-factory-backend -f
# 或者
pm2 logs paper-factory-backend

# 查看 Nginx 日志
sudo tail -f /var/log/nginx/error.log
sudo tail -f /var/log/nginx/access.log
```

## 已知问题和注意事项

1. **SSH 连接**: 本地环境 SSH 连接被拒绝（端口 22），可能服务器使用了非标准端口
2. **dist 权限**: 构建目录可能被 root 拥有，部署时需要修改权限
3. **Cloud Run 依赖**: 云端加速功能需要 gcloud CLI 和有效的 GCP 凭证
4. **首次冷启动**: Cloud Run 首次调用可能需要 30-60 秒

## 完成标志

部署成功的标志：
- ✅ https://tfisher.de/paper-factory/ 可访问
- ✅ 登录后能看到项目列表
- ✅ 咨询面板有 AI 工具卡片
- ✅ Step 5/6 能触发云端加速弹窗
- ✅ 后端 API 返回正确的 Cloud Run 状态
- ✅ 浏览器控制台无错误

---

**构建时间**: 2026-06-23 08:55
**分支**: modeling-factory
**前端版本**: 1.0.0
**新增功能**: 人工咨询快捷入口 + GCP Cloud Run 加速询问
