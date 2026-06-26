# 🎉 Web Dashboard 部署完成报告

## 部署概况

**部署日期**: 2026-06-16
**域名**: https://tfisher.de
**状态**: ✅ 部署成功并通过测试

## 部署架构

```
Internet
    ↓
  HTTPS (443)
    ↓
  Nginx (反向代理)
    ├─→ 静态文件 (/var/www/tfisher.de/)
    └─→ API 代理 (/api → http://127.0.0.1:8000)
         ↓
    FastAPI + Uvicorn (systemd service)
         ↓
    Paper Factory Backend
```

## 部署组件

### 1. 前端 (Vue 3 + Vite)
- **位置**: `/var/www/tfisher.de/`
- **服务器**: Nginx
- **构建**: `npm run build` → dist/
- **部署**: 复制 dist/ 到 /var/www/tfisher.de/

### 2. 后端 (FastAPI)
- **服务**: `paper-factory-api.service`
- **端口**: 8000 (内部)
- **用户**: tfisher
- **工作目录**: `/home/tfisher/paper_factory/web/backend`
- **Python 环境**: `/home/tfisher/paper_factory/web/backend/venv`
- **启动命令**: `uvicorn app:app --host 0.0.0.0 --port 8000`

### 3. Nginx 反向代理
- **配置文件**: `/etc/nginx/sites-available/tfisher.de`
- **API 代理**: `/api → http://127.0.0.1:8000`
- **WebSocket**: `/ws → http://127.0.0.1:8000/ws`
- **静态文件**: `/` → `/var/www/tfisher.de/`

### 4. SSL 证书
- **提供商**: Let's Encrypt
- **自动续期**: Certbot
- **证书路径**: `/etc/letsencrypt/live/tfisher.de/`

## 已通过的测试

✅ 前端 HTTPS 访问
✅ 用户登录认证
✅ 文件上传功能
✅ 项目创建功能
✅ 项目查询功能
✅ 后端服务稳定性

## 访问信息

**URL**: https://tfisher.de

**登录凭据**:
- 用户名: `admin`
- 密码: `T-fisher2005`

## 管理命令

### 查看服务状态
```bash
sudo systemctl status paper-factory-api
```

### 重启后端
```bash
sudo systemctl restart paper-factory-api
```

### 查看日志
```bash
# 实时日志
sudo journalctl -u paper-factory-api -f

# 输出日志
tail -f /home/tfisher/paper_factory/logs/api.log

# 错误日志
tail -f /home/tfisher/paper_factory/logs/api.error.log
```

### 重新加载 Nginx
```bash
sudo nginx -t
sudo systemctl reload nginx
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
# 修改代码后
sudo systemctl restart paper-factory-api

# 更新依赖后
cd /home/tfisher/paper_factory/web/backend
source venv/bin/activate
pip install -r ../requirements.txt
sudo systemctl restart paper-factory-api
```

### 更新环境变量
```bash
nano /home/tfisher/paper_factory/web/.env
# 修改后重启服务
sudo systemctl restart paper-factory-api
```

## 文件位置

- **前端静态文件**: `/var/www/tfisher.de/`
- **后端代码**: `/home/tfisher/paper_factory/web/backend/`
- **虚拟环境**: `/home/tfisher/paper_factory/web/backend/venv/`
- **环境变量**: `/home/tfisher/paper_factory/web/.env`
- **上传文件**: `/home/tfisher/paper_factory/uploads/`
- **项目目录**: `/home/tfisher/paper_factory/ongoing/`
- **日志文件**: `/home/tfisher/paper_factory/logs/`
- **Nginx 配置**: `/etc/nginx/sites-available/tfisher.de`
- **Systemd 服务**: `/etc/systemd/system/paper-factory-api.service`

## 安全配置

✅ HTTPS 强制重定向
✅ JWT 认证保护所有 API
✅ 密码 SHA256 哈希存储
✅ 文件类型白名单验证
✅ 文件大小限制 (100MB)
✅ Systemd 服务隔离

## 性能优化

✅ Gzip 压缩
✅ 静态资源缓存 (1年)
✅ HTTP/2 支持
✅ Keep-Alive 连接

## 监控建议

1. 定期检查服务状态
2. 监控磁盘空间（上传文件和日志）
3. 定期备份配置文件
4. 检查 SSL 证书过期时间

## 故障排查

### 前端无法访问
```bash
sudo systemctl status nginx
sudo nginx -t
sudo tail -f /var/log/nginx/error.log
```

### 后端 API 错误
```bash
sudo systemctl status paper-factory-api
tail -f /home/tfisher/paper_factory/logs/api.error.log
curl http://127.0.0.1:8000/
```

### 登录失败
```bash
cat /home/tfisher/paper_factory/web/.env
sudo systemctl restart paper-factory-api
```

## 下一步

1. ✅ 部署完成
2. ✅ 测试通过
3. 可选：设置监控告警
4. 可选：配置数据库持久化
5. 可选：添加更多用户

---

**部署完成！** 🎉

系统已成功部署到 https://tfisher.de 并通过全部测试。
