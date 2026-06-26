# 部署完成报告

## ✅ 部署状态

Paper Factory Web Dashboard 已成功部署到域名：**https://tfisher.de**

## 部署信息

### 域名和 SSL
- 主域名：https://tfisher.de
- 备用域名：https://www.tfisher.de
- SSL 证书：Let's Encrypt（自动续期）
- HTTP → HTTPS：自动重定向

### 前端部署
- 位置：`/var/www/tfisher.de/`
- 服务器：nginx
- 构建工具：Vite
- 部署时间：2026-06-15 02:24

### 后端部署
- 服务名：`paper-factory-api.service`
- 运行端口：8000（内部）
- 进程管理：systemd
- 用户：tfisher
- 工作目录：`/home/tfisher/paper_factory/web/backend`
- Python 环境：`/home/tfisher/paper_factory/web/backend/venv`
- 日志：
  - 标准输出：`/home/tfisher/paper_factory/logs/api.log`
  - 错误输出：`/home/tfisher/paper_factory/logs/api.error.log`

### nginx 配置
- 配置文件：`/etc/nginx/sites-available/tfisher.de`
- API 反向代理：`/api/*` → `http://127.0.0.1:8000/api/*`
- WebSocket 支持：`/ws` → `http://127.0.0.1:8000/ws`
- 静态文件：`/` → `/var/www/tfisher.de/`
- Gzip 压缩：已启用
- 静态资源缓存：1 年

## 测试结果

### ✅ 前端测试
- [x] HTTPS 访问正常
- [x] 静态文件正确部署
- [x] Vue Router 工作正常

### ✅ 后端测试
- [x] API 服务运行中
- [x] 登录 API 响应正确
- [x] systemd 服务自动重启

### ✅ 反向代理测试
- [x] nginx 配置正确
- [x] API 代理工作
- [x] WebSocket 支持已配置

## 访问方式

### 用户访问
1. 打开浏览器访问：https://tfisher.de
2. 使用默认账号登录：
   - 用户名：`admin`
   - 密码：`admin123`
3. 点击"➕ 新建项目"创建建模任务

### 管理员操作

#### 查看服务状态
```bash
sudo systemctl status paper-factory-api
```

#### 重启服务
```bash
sudo systemctl restart paper-factory-api
```

#### 查看日志
```bash
# 实时日志
sudo journalctl -u paper-factory-api -f

# API 输出日志
tail -f /home/tfisher/paper_factory/logs/api.log

# API 错误日志
tail -f /home/tfisher/paper_factory/logs/api.error.log
```

#### 停止/启动服务
```bash
sudo systemctl stop paper-factory-api
sudo systemctl start paper-factory-api
```

## 更新流程

### 更新前端
```bash
cd /home/tfisher/paper_factory/web/frontend
npm run build
sudo rm -rf /var/www/tfisher.de/*
sudo cp -r dist/* /var/www/tfisher.de/
sudo chown -R www-data:www-data /var/www/tfisher.de/
```

### 更新后端
```bash
cd /home/tfisher/paper_factory
git pull  # 如果使用 git

# 重启服务
sudo systemctl restart paper-factory-api
```

### 更新 nginx 配置
```bash
sudo nano /etc/nginx/sites-available/tfisher.de
sudo nginx -t
sudo systemctl reload nginx
```

## 安全配置

### 修改管理员密码
```bash
cd /home/tfisher/paper_factory/web
nano .env
# 修改 ADMIN_PASSWORD=your_new_password

sudo systemctl restart paper-factory-api
```

### 添加用户
编辑 `/home/tfisher/paper_factory/web/backend/app.py` 中的 `USERS_DB` 字典，然后重启服务。

### 防火墙
确保只开放必要的端口：
- 80 (HTTP)
- 443 (HTTPS)
- 22 (SSH)

端口 8000 应该只在内部访问，不对外开放。

## 监控和维护

### systemd 服务
- 服务会在系统启动时自动启动
- 如果崩溃会自动重启（RestartSec=10）
- 日志自动记录到指定文件

### SSL 证书续期
Let's Encrypt 证书有效期 90 天，由 certbot 自动续期。检查续期状态：
```bash
sudo certbot certificates
```

### 磁盘空间
定期清理日志文件：
```bash
# 清理旧日志（保留最近 1000 行）
tail -1000 /home/tfisher/paper_factory/logs/api.log > /tmp/api.log
mv /tmp/api.log /home/tfisher/paper_factory/logs/api.log

tail -1000 /home/tfisher/paper_factory/logs/api.error.log > /tmp/api.error.log
mv /tmp/api.error.log /home/tfisher/paper_factory/logs/api.error.log
```

## 故障排查

### 前端无法访问
1. 检查 nginx 状态：`sudo systemctl status nginx`
2. 检查配置：`sudo nginx -t`
3. 查看 nginx 错误日志：`sudo tail -f /var/log/nginx/error.log`

### 后端 API 错误
1. 检查服务状态：`sudo systemctl status paper-factory-api`
2. 查看错误日志：`tail -f /home/tfisher/paper_factory/logs/api.error.log`
3. 手动测试：`curl http://127.0.0.1:8000/`

### 登录失败
1. 检查 `.env` 文件是否存在
2. 确认密码正确
3. 查看后端日志

### WebSocket 连接失败
1. 检查 nginx 配置中的 WebSocket 部分
2. 确认后端服务运行正常
3. 查看浏览器控制台错误

## 性能优化

### 启用 HTTP/2
nginx 配置已支持 HTTP/2（listen 443 ssl http2）

### 静态资源缓存
- JS/CSS/图片：1 年缓存
- HTML：无缓存（立即更新）

### Gzip 压缩
已启用，压缩文本资源（HTML、CSS、JS、JSON）

## 备份建议

### 配置文件
```bash
# 备份重要配置
cp /etc/nginx/sites-available/tfisher.de ~/backups/nginx-tfisher.de
cp /etc/systemd/system/paper-factory-api.service ~/backups/
cp /home/tfisher/paper_factory/web/.env ~/backups/
```

### 数据库（如有）
目前使用内存数据库，重启后数据不保留。如需持久化，考虑添加 PostgreSQL。

## 联系方式

管理员：tfisher
服务器：v2202602338287434826.quicksrv.de
部署时间：2026-06-15 02:25 UTC

---

✅ 部署完成！访问 https://tfisher.de 开始使用
