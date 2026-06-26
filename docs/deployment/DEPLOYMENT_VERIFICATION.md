# 部署验证清单

**部署时间：** 2026-06-23 09:06 UTC  
**部署路径：** https://tfisher.de/ 和 https://tfisher.de/paper-factory/

## ✅ 后端服务状态

- [x] 后端进程运行中（PID: 2009688, uvicorn on port 8000）
- [x] API 健康检查响应正常（`/api/health`）
- [x] 认证机制工作（`/api/projects` 返回未认证错误）
- [x] Nginx 反向代理配置正确

## ✅ 前端资源同步

- [x] `index.html` MD5 一致（078a6779236a1ade10a7eaf72a8b97e7）
- [x] JavaScript 包：`index-De5FzPpK.js`（472KB）
- [x] CSS 样式：`index-DHcvl8du.css`（81KB）
- [x] 旧版本资源已清理
- [x] 文件权限正确（www-data:www-data）

## 🎯 新功能验证步骤

### 1. 基础访问测试

打开浏览器访问：
- https://tfisher.de/ （根路径）
- https://tfisher.de/paper-factory/ （子路径）

两个路径应该显示相同的界面标题："Paper Factory — 建模工坊控制台"

### 2. 控制台功能验证

**a) 咨询面板 (ConsultationPanel)**
   - 在项目详情页查看是否有咨询请求面板
   - 检查咨询状态显示（AWAITING/READY/COMPLETED）
   - 测试咨询回复提交功能

**b) 新建项目模态框 (NewProjectModal)**
   - 点击"新建项目"按钮
   - 检查文件上传功能
   - 验证项目创建流程
   - 确认 `--consult` 选项是否可配置

**c) 项目工作区 (ProjectWorkspace)**
   - 打开任意项目
   - 查看项目状态仪表盘
   - 检查步骤进度显示
   - 验证日志查看功能
   - 测试项目操作按钮（Resume/Pause/Consult）

### 3. 模型管理功能验证

**检查要点：**
- 每步模型选择界面是否正常显示
- 模型配置是否可编辑
- 模型选择是否持久化到 `model_config.json`
- 不同步骤的模型设置是否独立

验证文件：
```bash
cat ~/paper_factory/web/model_registry.json
cat ~/paper_factory/ongoing/<project>/model_config.json
```

### 4. API 功能测试

在浏览器开发者工具 Console 中执行：

```javascript
// 测试项目列表
fetch('/api/projects').then(r => r.json()).then(console.log)

// 测试健康检查
fetch('/api/health').then(r => r.json()).then(console.log)

// 测试 WebSocket 连接
const ws = new WebSocket('wss://tfisher.de/ws')
ws.onopen = () => console.log('WebSocket 已连接')
ws.onmessage = (e) => console.log('收到消息:', e.data)
```

### 5. 性能检查

**加载速度：**
- F12 打开开发者工具 → Network 标签
- 刷新页面，检查资源加载时间
- JS 包应该 < 2 秒加载完成
- 总页面加载时间应该 < 3 秒

**浏览器兼容性：**
- [x] Chrome/Edge
- [ ] Firefox
- [ ] Safari

### 6. 数据对比验证

**修改内容统计（来自 git diff）：**
- `ConsultationPanel.vue`: +66 行（新增咨询面板）
- `NewProjectModal.vue`: +14/-9 行（增强项目创建）
- `ProjectWorkspace.vue`: +44/+1 行（扩展工作区功能）

**代码中应包含的新功能关键字：**
- ✓ `ConsultationPanel`（咨询面板组件）
- ✓ `NewProjectModal`（新建项目弹窗）
- ✓ `ProjectWorkspace`（项目工作区）
- 预期：`ModelManagement`（模型管理，检查中...）

## 🔍 故障排查

### 问题：页面显示空白或 404

**检查：**
```bash
# 1. 验证文件存在
ls -lh /var/www/tfisher.de/index.html
ls -lh /var/www/tfisher.de/assets/

# 2. 检查 Nginx 配置
sudo nginx -t

# 3. 查看 Nginx 错误日志
sudo tail -f /var/log/nginx/error.log

# 4. 检查文件权限
ls -la /var/www/tfisher.de/
```

### 问题：API 请求失败

**检查：**
```bash
# 1. 后端进程状态
ps aux | grep uvicorn

# 2. 后端日志
tail -f ~/paper_factory/web/backend/app.log

# 3. 端口监听
netstat -tlnp | grep 8000

# 4. 手动测试 API
curl http://127.0.0.1:8000/api/health
```

### 问题：旧版本缓存

**解决方法：**
- 硬刷新：Ctrl+Shift+R (Linux/Windows) 或 Cmd+Shift+R (Mac)
- 清除浏览器缓存
- 无痕模式测试

## 📊 部署前后对比

| 指标 | 旧版本 | 新版本 | 变化 |
|------|--------|--------|------|
| JS 包大小 | 462KB | 472KB | +10KB (+2.2%) |
| CSS 大小 | 75KB | 81KB | +6KB (+8%) |
| 组件文件 | 2 个 | 3 个 | +1 (ConsultationPanel) |
| 代码行数 | - | - | +115 行 |

## ✅ 最终确认

部署成功的标志：
1. [x] 两个 URL 返回相同的页面标题
2. [x] 资源文件 MD5 完全一致
3. [x] 后端 API 响应正常
4. [ ] 浏览器中所有新功能可访问（需人工测试）
5. [ ] 无 JavaScript 控制台错误（需人工验证）

---

**下一步行动：**
1. 在浏览器中打开 https://tfisher.de/
2. 按照上述清单逐项验证功能
3. 如有问题，参考故障排查部分
