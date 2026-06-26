# Web 界面增强功能说明

## 新增功能

### 1. 人工咨询快捷入口

**位置**: ConsultationPanel（咨询面板）

**功能**: 当项目需要人工咨询时（consultation_pending = true），咨询面板中会显示三个AI工具的快捷入口：

- **ChatGPT Pro** (GPT-4) - 用于深度分析和推理
- **Gemini Deep Think** - Google 的高级推理模型
- **Claude Code** - 编码和分析辅助

**使用流程**:
1. 点击任一AI工具卡片，在新标签页打开对应工具
2. 将咨询内容复制粘贴到AI工具中进行分析
3. 将AI工具的结论粘贴回表单提交
4. 提交后项目自动恢复运行

### 2. GCP Cloud Run 加速计算弹窗

**触发条件**: 
- 项目进入计算密集型步骤（Step 5: 求解，Step 6: 敏感性分析）
- 延迟 5 秒后自动弹出询问

**功能特性**:
- **实时状态检查**: 自动检测 GCP Cloud Run 服务可用性
- **效果预估**: 显示本地串行 vs 云端并行的耗时对比
- **配置展示**: 显示服务区域、并发实例数、支持的求解器类型
- **一键启用**: 点击按钮即可为当前项目启用云端加速

**预估效果**:
- Step 5 (求解): 本地 6h → 云端 1.5h (4× 加速)
- Step 6 (敏感性): 本地 8h → 云端 2h (4× 加速)

**配置方式**:
- 启用后在项目目录创建 `.env.cloud` 文件
- 配置参数:
  - `USE_CLOUD_SOLVER=true`
  - `CLOUD_THRESHOLD_TIME=300` (超过5分钟的任务使用云端)
  - `CLOUD_SOLVER_TYPES=python,julia,matlab,R`
  - GCP 项目信息（project_id, region, service_name）

**工作原理**:
- 本地 `solver_router.sh` 根据任务时长判断是否使用云端
- 长时间任务自动路由到 `gcp_solver_client.sh`
- 通过 Cloud Run API 提交任务并获取结果
- 支持并行执行多个求解任务

## 后端 API 新增端点

### Cloud Run 状态查询
```
GET /api/cloud/status
```
返回 Cloud Run 服务可用性、区域、最大实例数等信息

### Cloud Run 配置查询
```
GET /api/cloud/config
```
返回当前云端求解器配置（阈值、支持类型等）

### 启用云端加速
```
POST /api/projects/{base_name}/cloud/enable
```
为指定项目启用云端加速，创建 `.env.cloud` 配置文件

### 禁用云端加速
```
POST /api/projects/{base_name}/cloud/disable
```
禁用云端加速，删除 `.env.cloud` 配置文件

## 前端组件

### 新增组件
- `CloudAcceleratorDialog.vue` - 云端加速询问对话框
- 更新 `ConsultationPanel.vue` - 添加 AI 工具快捷入口
- 更新 `ProjectWorkspace.vue` - 集成云端加速逻辑

### API 客户端
- 新增 `Cloud` API 模块在 `api.js` 中
- 提供 `status()`, `enable()`, `disable()`, `config()` 方法

## 使用示例

### 场景 1: 人工咨询
1. 项目运行到 Step 0 (preflight) 或 Step 4，需要人工决策
2. 控制台显示"等待咨询"状态，咨询面板自动展开
3. 查看咨询内容，点击"Gemini Deep Think"打开新标签
4. 在 Gemini 中分析问题，获得建议方案
5. 复制结论回到表单，点击"提交并恢复运行"
6. 项目自动继续执行

### 场景 2: 云端加速
1. 项目运行到 Step 5 (Full Solve)
2. 5 秒后弹出"云端加速计算"对话框
3. 查看预估效果：本地 6h → 云端 1.5h (4× 加速)
4. 确认 Cloud Run 服务状态为"可用"
5. 点击"启用云端加速"
6. 后续长时间求解任务自动分发到 Cloud Run 并行执行

## 配置要求

### GCP Cloud Run 服务
- 已部署 solver-api 服务（见 `CLOUD_DEPLOYMENT_SUCCESS.md`）
- 配置 `.env.gcp` 或环境变量:
  ```bash
  GCP_PROJECT_ID=level-night-476302-k0
  GCP_REGION=europe-west4
  GCP_SOLVER_SERVICE=solver-api
  ```

### gcloud CLI
- 需要安装 `gcloud` 命令行工具
- 已认证: `gcloud auth login`
- 已设置默认项目: `gcloud config set project <project_id>`

## 注意事项

1. **费用**: 云端计算按 Cloud Run 实例运行时间计费
2. **网络**: 需要上传脚本和下载结果，对网络连接有要求
3. **冷启动**: 首次调用可能需要 30-60 秒启动时间
4. **配置作用域**: 云端加速配置是项目级别的，可随时禁用
5. **自动判断**: 只有超过阈值（默认 5 分钟）的任务才会使用云端

## 测试建议

1. 先在测试项目上启用云端加速，观察行为
2. 检查 `logs/runner.log` 中的 `[solver_router]` 日志
3. 验证短任务仍在本地执行，长任务路由到云端
4. 监控 GCP Cloud Run 控制台的实例启动情况
