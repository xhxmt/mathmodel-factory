# 部署到域名 - 总结

## ✅ 部署完成

Paper Factory Web Dashboard 已成功部署到：**https://tfisher.de**

## 访问信息

- **URL**: https://tfisher.de
- **用户名**: admin
- **密码**: admin123

## 部署架构

```
Internet
   ↓
nginx (443/80)
   ↓
├─→ /          → /var/www/tfisher.de/ (前端静态文件)
├─→ /api/*     → 127.0.0.1:8000/api/* (后端 API)
└─→ /ws        → 127.0.0.1:8000/ws    (WebSocket)
                     ↓
              paper-factory-api.service
                (systemd 管理)
```

## 部署内容

### 前端
- **位置**: `/var/www/tfisher.de/`
- **服务器**: nginx
- **SSL**: Let's Encrypt

### 后端
- **服务**: `paper-factory-api.service`
- **端口**: 8000（仅内部访问）
- **管理**: systemd（自动启动、自动重启）
- **日志**:
  - `/home/tfisher/paper_factory/logs/api.log`
  - `/home/tfisher/paper_factory/logs/api.error.log`

## 快速命令

### 查看状态
```bash
sudo systemctl status paper-factory-api
```

### 重启服务
```bash
sudo systemctl restart paper-factory-api
```

### 查看日志
```bash
# 实时日志
sudo journalctl -u paper-factory-api -f

# 应用日志
tail -f /home/tfisher/paper_factory/logs/api.log
```

### 重新部署
```bash
# 完整部署（前端+后端）
cd /home/tfisher/paper_factory/web
sudo ./deploy.sh

# 仅更新后端
./deploy.sh backend-only
```

### 手动更新前端
```bash
cd /home/tfisher/paper_factory/web/frontend
npm run build
sudo rm -rf /var/www/tfisher.de/*
sudo cp -r dist/* /var/www/tfisher.de/
sudo chown -R www-data:www-data /var/www/tfisher.de/
```

## 配置文件

- nginx: `/etc/nginx/sites-available/tfisher.de`
- systemd: `/etc/systemd/system/paper-factory-api.service`
- 环境变量: `/home/tfisher/paper_factory/web/.env`

## 安全提醒

### 修改密码
```bash
cd /home/tfisher/paper_factory/web
nano .env
# 修改 ADMIN_PASSWORD=your_password
sudo systemctl restart paper-factory-api
```

### 防火墙
确保端口 8000 不对外开放，仅通过 nginx 反向代理访问。

## 文档

- 📖 完整部署文档: `web/DEPLOYMENT.md`
- 🚀 快速开始: `web/QUICKSTART.md`
- 📚 使用手册: `web/README.md`
- 📝 实现总结: `web/IMPLEMENTATION_SUMMARY.md`

## 功能特性

✅ 登录认证（JWT Token）
✅ Web 界面创建项目
✅ 实时项目监控
✅ 项目控制（暂停/恢复/终止）
✅ 人工咨询处理
✅ WebSocket 实时更新
✅ HTTPS 加密传输
✅ 自动服务管理

---

**立即访问**: https://tfisher.de
