# 人工咨询信息增强 - 测试指南

## 改进内容

### 1. 后端增强 (`run_paper.sh`)

原来的 `maybe_consult()` 函数生成的咨询请求仅包含：
- 基本元数据（gate、step、project、created）
- 简单的咨询问题文本

**现在增强为**包含以下丰富信息：

#### 📋 项目背景
- 问题概要（从 `problem_brief.md` 提取前 30 行）
- 当前步骤名称（如"完整模型构建"、"并行建模提案"等）

#### 🎯 决策影响分析
根据不同的 gate 类型提供定制化的影响说明：
- **preflight**: 影响整个项目的技术路线、研究方向
- **step4**: 影响模型公式化、求解方案、验证策略
- **dynamic**: agent 主动求助时的上下文

每个 gate 还包含"建议关注点"，指导外部模型从哪些角度分析。

#### 📁 关键文件引用
自动扫描并列出项目中已完成的关键文件：
- `problem/problem_brief.md` — 问题解析
- `viable_streams.md` — 可行方法流
- `m1_spec.md`, `m2_spec.md` — 建模提案
- `model.md` — 完整模型描述
- `symbol_table.md` — 符号表
- 等等...

#### 💡 回答建议
明确告诉外部模型应该提供什么：
1. 明确的决策建议
2. 充分的理由
3. 潜在风险提示
4. 实施要点

### 2. API 增强 (`web/backend/app.py`)

**ConsultationRequest 模型扩展**：
```python
class ConsultationRequest(BaseModel):
    gate: str
    step: int
    title: str
    content: str              # 核心问题
    project: str
    created: str
    background: Optional[str]    # 新增：项目背景
    impact: Optional[str]        # 新增：决策影响
    key_files: Optional[List[str]]  # 新增：关键文件列表
    suggestions: Optional[str]   # 新增：回答建议
```

**get_consultation_request() 函数增强**：
- 使用正则表达式从增强的 markdown 中提取各个章节
- 支持新格式的 emoji 标题（📋、🎯、📁、💡）
- 向后兼容旧格式
- 自动解析文件列表中的 markdown 链接

### 3. 前端展示增强 (`web/frontend/src/components/ProjectDetailModal.vue`)

#### UI 改进
- **分段展示**：不再是一大段文本，而是按章节组织
- **视觉层次**：使用不同的卡片区块和图标
- **核心问题高亮**：用蓝色背景突出显示核心咨询问题
- **文件列表**：用文件图标 + 代码字体展示关键文件
- **富文本渲染**：支持 markdown 渲染（标题、列表、粗体、代码块）

#### 新增样式类
- `.consultation-section` — 各个信息区块
- `.consultation-section.highlight` — 核心问题区块（蓝色高亮）
- `.rich-content` — 富文本内容容器
- `.file-list` / `.file-item` — 文件列表样式

#### Markdown 渲染
新增 `renderMarkdown()` 函数，支持：
- 代码块 (\`\`\`)
- 行内代码 (\`)
- 粗体 (**)
- 标题 (#、##、###)
- 列表 (-)

## 测试步骤

### 1. 启动一个新项目（带咨询功能）

```bash
cd /home/tfisher/paper_factory
./launch_agents.sh new --consult test_consult_2024 /path/to/problem.pdf
```

### 2. 等待到达咨询节点

项目会在以下节点之一暂停：
- **preflight** (Step 0 后): 启动前 seed
- **step4** (Step 4 前): 建模定型前
- **dynamic**: agent 主动求助时

### 3. 查看生成的咨询请求文件

```bash
cat ongoing/test_consult_2024/consultation/preflight_request.md
# 或
cat ongoing/test_consult_2024/consultation/step4_request.md
```

**应该看到**：
- ✅ 清晰的章节划分（📋、🎯、📁、🤔、💡）
- ✅ 项目背景自动填充
- ✅ 决策影响分析
- ✅ 关键文件列表
- ✅ 回答建议

### 4. 启动 Web Dashboard

```bash
cd /home/tfisher/paper_factory/web
python3 backend/app.py &
cd frontend
npm run dev
```

访问 http://localhost:5173

### 5. 在 Web 界面中查看咨询请求

1. 登录（默认 admin/admin123）
2. 点击项目卡片，打开详情 Modal
3. 切换到"人工咨询"标签

**应该看到**：
- ✅ 项目背景区块（灰色背景）
- ✅ 决策影响区块（灰色背景）
- ✅ 关键文件列表（文件图标 + 蓝色边框）
- ✅ 核心问题区块（**蓝色高亮背景**）
- ✅ 回答建议区块（灰色背景）
- ✅ 各区块有清晰的 emoji 图标和标题
- ✅ Markdown 格式正确渲染（列表、代码、粗体等）

### 6. 向后兼容性测试

如果有旧的咨询请求文件（不含增强信息），API 应该：
- ✅ 正常解析核心问题
- ✅ 可选字段为 `null`
- ✅ 前端只显示核心问题，不报错

## 预期效果对比

### 改进前
```
## 需要你（借助 GPT Pro / Gemini Deep Think）决定的事

启动前 seed：题目解读与候选方法（贴 GPT Pro / Gemini Deep Think 的初步结论）

## 回填方式
...
```

用户看到的信息：一句话描述，没有上下文，不知道该从何入手。

### 改进后

**用户看到的是一个结构化的咨询包**：

1. **📋 项目背景**
   - 问题的前 30 行摘要
   - 当前所在步骤的明确说明

2. **🎯 决策影响**
   - 这个决策会影响哪些后续步骤
   - 建议关注哪些技术点

3. **📁 关键文件**
   - 已完成的 5-10 个关键文件列表
   - 可以快速了解项目进展

4. **🤔 核心问题**（高亮显示）
   - 具体的咨询问题

5. **💡 回答建议**
   - 结构化的回答模板
   - 明确告知应该提供什么类型的信息

## 回滚方案

如果需要回滚到旧版本：

```bash
cd /home/tfisher/paper_factory
git diff run_paper.sh
git diff web/backend/app.py
git diff web/frontend/src/components/ProjectDetailModal.vue

# 如果需要恢复
git checkout HEAD -- run_paper.sh
git checkout HEAD -- web/backend/app.py
git checkout HEAD -- web/frontend/src/components/ProjectDetailModal.vue
```

## 注意事项

1. **向后兼容**：旧的咨询请求文件仍能正常工作，只是不显示增强信息
2. **性能影响**：增加了一些文件读取操作（problem_brief.md、文件扫描），但都在秒级完成
3. **Markdown 渲染**：前端使用简化的 markdown 渲染器，支持基本语法，不支持复杂的 markdown 特性

## 未来改进方向

1. **可下载完整上下文包**：一键下载所有关键文件的 zip 包
2. **集成 AI 建议**：直接在 Web 界面调用 GPT Pro / Gemini API
3. **历史咨询记录**：保存所有咨询的历史及回答
4. **咨询模板库**：针对常见问题提供预设模板
