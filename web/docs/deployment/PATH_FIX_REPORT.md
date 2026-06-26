# PATH 环境变量修复报告

## 问题描述

**错误信息**：
```
500: Failed to create project: /home/tfisher/paper_factory/launch_agents.sh: 行 4: dirname: 未找到命令
/home/tfisher/paper_factory/launch_agents.sh: 行 14: mkdir: 未找到命令
```

**根本原因**：
systemd 服务 `paper-factory-api.service` 的 `PATH` 环境变量只包含 venv 路径：
```
Environment="PATH=/home/tfisher/paper_factory/web/backend/venv/bin"
```

当后端通过 `subprocess.run()` 调用 `launch_agents.sh` 时，子进程继承了这个残缺的 `PATH`，导致脚本内的基本命令（`dirname`, `mkdir`, `cd` 等）无法找到。

## 修复方案

在所有 `subprocess.run()` 调用中显式传入完整的 `PATH` 环境变量。

### 修改的代码位置

**文件**：`web/backend/app.py`

#### 1. 创建项目端点（~行 880）
```python
# 修复前
result = subprocess.run(
    cmd,
    capture_output=True,
    text=True,
    timeout=30,
    cwd=FACTORY_ROOT
)

# 修复后
env = os.environ.copy()
env["PATH"] = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
result = subprocess.run(
    cmd,
    capture_output=True,
    text=True,
    timeout=30,
    cwd=FACTORY_ROOT,
    env=env
)
```

#### 2. 读取日志端点（~行 973）
```python
# 修复前
result = subprocess.run(
    ["tail", "-n", str(lines), str(recent_log)],
    capture_output=True,
    text=True
)

# 修复后
env = os.environ.copy()
env["PATH"] = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
result = subprocess.run(
    ["tail", "-n", str(lines), str(recent_log)],
    capture_output=True,
    text=True,
    env=env
)
```

#### 3. 项目操作端点（~行 1067）
```python
# 修复前
result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)

# 修复后
env = os.environ.copy()
env["PATH"] = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
result = subprocess.run(cmd, capture_output=True, text=True, timeout=10, env=env)
```

## 完整的 PATH

修复后的 PATH 包含所有标准系统路径：
- `/usr/local/sbin` - 本地管理员命令
- `/usr/local/bin` - 本地用户命令
- `/usr/sbin` - 系统管理员命令
- `/usr/bin` - 系统用户命令
- `/sbin` - 系统基本管理命令
- `/bin` - 系统基本用户命令

这确保了 `launch_agents.sh` 中使用的所有命令都能找到：
- `dirname` → `/usr/bin/dirname`
- `mkdir` → `/usr/bin/mkdir`
- `cd` → shell 内建命令
- `grep`, `awk`, `sed` 等其他工具

## 部署状态

- ✅ 代码已修复（3处 subprocess.run 调用）
- ✅ 后端服务已重启（PID: 1778862）
- ✅ 服务运行正常
- ⏳ 待用户在 tfisher.de 上测试

## 测试步骤

1. 访问 https://tfisher.de
2. 登录系统
3. 创建新项目
4. 上传 2025 年 A 题压缩包或 PDF
5. 验证项目创建成功，无 500 错误

## 预防措施

### 方案1：修改 systemd 服务配置（推荐）

编辑 `/etc/systemd/system/paper-factory-api.service`：
```ini
[Service]
Type=simple
User=tfisher
Group=tfisher
WorkingDirectory=/home/tfisher/paper_factory/web/backend
Environment="PATH=/home/tfisher/paper_factory/web/backend/venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
```

然后：
```bash
sudo systemctl daemon-reload
sudo systemctl restart paper-factory-api
```

**优点**：一劳永逸，所有子进程自动继承完整 PATH  
**缺点**：需要 sudo 权限修改系统配置

### 方案2：保持当前代码修复（已实施）

在每个 `subprocess.run()` 调用时显式传入 `env`。

**优点**：无需修改系统配置  
**缺点**：需要记得在所有新增的 subprocess 调用中也添加

## 相关文件

- `web/backend/app.py` - 已修复
- `/etc/systemd/system/paper-factory-api.service` - systemd 配置（可选优化）

## 时间线

- **2026-06-21 02:00** - 用户报告 500 错误
- **2026-06-21 02:10** - 诊断 PATH 问题
- **2026-06-21 02:15** - 修复代码
- **2026-06-21 02:16** - 重启服务
- **2026-06-21 02:17** - 服务运行正常

## 验证

```bash
# 检查服务状态
sudo systemctl status paper-factory-api

# 查看日志（如有问题）
sudo journalctl -u paper-factory-api -f

# 测试 API 响应
curl http://127.0.0.1:8000/
```

---

**状态**：✅ 已修复并部署  
**影响**：修复了创建项目功能  
**兼容性**：无破坏性变更
