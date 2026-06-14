# Web Dashboard 截图

## 主界面

### 项目列表视图
![主界面](screenshots/main_dashboard.png)

**包含元素**：
- 顶部统计栏（运行中、等待咨询、已完成）
- 项目卡片网格
- 实时状态指示器
- 控制按钮（暂停/恢复/终止）

---

## 项目详情弹窗

### 概览标签页
![概览](screenshots/detail_overview.png)

显示：
- 项目核心信息（状态、步骤、进度、PID）
- checkpoint.md 完整内容

### 日志标签页
![日志](screenshots/detail_logs.png)

显示：
- 最近 100 行执行日志
- 刷新按钮
- 终端风格显示

### 人工咨询标签页
![咨询](screenshots/detail_consultation.png)

显示：
- 咨询请求详情（gate、步骤、标题、内容）
- 回答提交表单
- 示例格式提示

---

## 状态展示

### 运行中项目
![运行中](screenshots/status_running.png)

特征：
- 蓝色状态点跳动
- 实时进度更新
- 显示 PID

### 等待咨询项目
![等待咨询](screenshots/status_consultation.png)

特征：
- 黄色状态点跳动
- 咨询警告框
- 标明 gate 名称

### 已完成项目
![已完成](screenshots/status_completed.png)

特征：
- 绿色状态点
- 100% 进度条
- 显示完成时间

---

## 交互动画

### 卡片悬停效果
![悬停效果](screenshots/card_hover.gif)

### WebSocket 实时更新
![实时更新](screenshots/realtime_update.gif)

### 咨询提交流程
![咨询流程](screenshots/consultation_flow.gif)

---

## 响应式设计

### 桌面端（>768px）
![桌面端](screenshots/responsive_desktop.png)

### 平板端（768px）
![平板端](screenshots/responsive_tablet.png)

### 移动端（<768px）
![移动端](screenshots/responsive_mobile.png)

---

## 暗色主题

所有界面采用统一的暗色主题：
- 背景：深蓝渐变（#0a0e27 → #1a1d35）
- 卡片：半透明深色（rgba(26, 29, 53, 0.6)）
- 文字：浅灰色（#e4e4e7）
- 强调色：蓝色（#60a5fa）、黄色（#fbbf24）、绿色（#34d399）

---

**注意**：截图目录 `web/screenshots/` 需要手动创建并添加实际截图。
使用浏览器的开发者工具（Device Mode）可以轻松获取不同设备尺寸的截图。
