# Quick Start Guide - 快速开始指南

## 第一步：配置

```bash
cd /home/tfisher/paper_factory/web
cp .env.example .env
```

编辑 `.env` 文件（可选，修改密码）：
```bash
nano .env
# 修改 ADMIN_PASSWORD=your_password
```

## 第二步：启动

```bash
./start_dashboard.sh
```

## 第三步：访问

浏览器打开：**http://localhost:5173**

登录凭据：
- 用户名：`admin`
- 密码：`admin123`（或你设置的密码）

## 第四步：创建项目

1. 点击右上角 **"➕ 新建项目"** 按钮
2. 填写：
   - 项目名称：`test_project_1`
   - 题目路径：`/path/to/problem.pdf`
3. 点击 **"创建项目"**

## 功能说明

### 登录系统
- ✅ JWT Token 认证
- ✅ 自动过期（24小时）
- ✅ 安全的密码存储

### 新建项目
- ✅ Web 界面创建
- ✅ 验证文件路径
- ✅ 可选：仅创建 / 启用咨询

### 项目管理
- ✅ 实时状态监控
- ✅ 暂停/恢复/终止
- ✅ 查看日志和详情
- ✅ 处理咨询请求

## 常用操作

### 修改密码
```bash
nano web/.env
# 修改 ADMIN_PASSWORD=new_password
# 重启服务
```

### 添加用户
编辑 `web/backend/app.py` 中的 `USERS_DB`

### 查看日志
浏览器 → 项目卡片 → 查看详情 → 日志标签页

### 处理咨询
浏览器 → 项目卡片 → 查看详情 → 人工咨询标签页

## 故障排查

### 登录失败
- 检查密码是否正确
- 检查 `.env` 文件是否存在
- 重启服务

### 创建项目失败
- 检查文件路径是否正确
- 检查 `launch_agents.sh` 权限
- 查看浏览器控制台错误

### WebSocket 断开
- 检查后端是否运行
- 刷新页面重连

## 更多信息

- 完整文档：`web/README.md`
- 实现总结：`web/IMPLEMENTATION_SUMMARY.md`
- API 文档：`web/README.md` 的 "API 端点" 章节
