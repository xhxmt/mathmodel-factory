# Web 界面改进需求清单 - 人工咨询功能

## 问题描述

当前 tfisher.de 的 Web 控制台在项目进入**人工咨询点**（CONSULT gate）时，缺少以下关键功能：

1. ❌ **没有显示咨询请求内容**：用户看不到系统具体需要咨询什么
2. ❌ **没有提供输入框**：无处填写咨询回答
3. ❌ **没有显示咨询提示词**：用户不知道该如何向 GPT Pro / Gemini Deep Think 询问
4. ❌ **没有状态更新提示**：用户不知道需要修改 `STATUS: AWAITING` → `STATUS: READY`

## 当前工作流程（命令行）

项目到达咨询点后，用户需要：

1. 查看咨询请求文件：
   ```bash
   cat ongoing/cumcm_2025_a/consultation/preflight_request.md
   ```

2. 使用提示词向 Gemini Deep Think 询问（提示词在项目目录）

3. 编辑 `human_review.md`：
   ```bash
   vim ongoing/cumcm_2025_a/human_review.md
   # 粘贴 Gemini 的回答到「你的回填」段落
   # 修改 STATUS: AWAITING → STATUS: READY
   ```

4. 恢复运行：
   ```bash
   ./launch_agents.sh resume cumcm_2025_a
   ```

## 需要的 Web 界面改进

### 1. 项目详情页 - 咨询面板增强

**当前状态**：项目卡片显示 `CONSULT(0)`，但点进去后咨询面板内容不完整

**需要添加**：

#### A. 咨询请求完整显示
```vue
<div class="consult-request">
  <h3>📋 咨询请求</h3>
  <div class="request-metadata">
    <span>Gate: {{ consultation.gate }}</span>
    <span>Step: {{ consultation.step }}</span>
    <span>创建时间: {{ consultation.created }}</span>
  </div>
  
  <div class="request-content">
    <!-- 渲染 consultation/preflight_request.md 的内容 -->
    <MarkdownRenderer :content="consultation.content" />
  </div>
</div>
```

#### B. 咨询提示词显示和复制
```vue
<div class="deepthink-prompt">
  <h3>💡 Gemini Deep Think 提示词</h3>
  <p class="hint">复制下面的提示词，粘贴到 Gemini Deep Think 界面进行分析</p>
  
  <div class="prompt-box">
    <pre>{{ geminiPrompt }}</pre>
    <button @click="copyPrompt" class="btn-copy">
      <Icon name="copy" />
      复制提示词
    </button>
  </div>
  
  <a href="https://gemini.google.com" target="_blank" class="btn-external">
    <Icon name="external-link" />
    打开 Gemini Deep Think
  </a>
</div>
```

#### C. 回答输入区域增强
```vue
<div class="consult-answer">
  <h3>✍️ 粘贴 Deep Think 的回答</h3>
  
  <textarea 
    v-model="answerText" 
    placeholder="将 Gemini Deep Think 的完整回答粘贴到这里..."
    rows="20"
    class="answer-textarea"
  ></textarea>
  
  <div class="answer-actions">
    <button @click="submitAnswer" class="btn btn-amber" :disabled="!answerText">
      <Icon name="check" />
      提交并恢复运行
    </button>
    <button @click="previewAnswer" class="btn btn-ghost">
      <Icon name="eye" />
      预览效果
    </button>
  </div>
  
  <div class="hint-box">
    ℹ️ 提交后系统会自动：
    <ul>
      <li>将回答写入 human_review.md</li>
      <li>修改状态为 STATUS: READY</li>
      <li>恢复项目运行（launch_agents.sh resume）</li>
    </ul>
  </div>
</div>
```

### 2. 后端 API 增强

#### A. 获取咨询提示词
```python
@app.get("/api/projects/{base_name}/consultation/prompt")
async def get_consultation_prompt(
    base_name: str,
    current_user: UserInfo = Depends(get_current_user)
):
    """Get the Gemini Deep Think prompt for consultation"""
    project_path = ONGOING_DIR / base_name
    
    # 读取项目中的 GEMINI_DEEPTHINK_PROMPT.md
    prompt_file = project_path / "GEMINI_DEEPTHINK_PROMPT.md"
    if not prompt_file.exists():
        # 如果不存在，使用默认模板
        prompt_file = FACTORY_ROOT / "prompts" / "consultation_prompt_template.md"
    
    return {
        "prompt": prompt_file.read_text(),
        "gemini_url": "https://gemini.google.com"
    }
```

#### B. 提交咨询回答（已有，需确认）
```python
@app.post("/api/projects/{base_name}/consultation/answer")
async def submit_consultation_answer(
    base_name: str,
    answer: ConsultationAnswer,
    current_user: UserInfo = Depends(get_current_user)
):
    """Submit consultation answer and resume project"""
    project_path = ONGOING_DIR / base_name
    human_review = project_path / "human_review.md"
    
    # 读取现有内容
    content = human_review.read_text() if human_review.exists() else ""
    
    # 找到 AWAITING 段落，替换为 READY 并填入回答
    # 正则匹配：## CONSULT preflight ... STATUS: AWAITING
    # 替换为：STATUS: READY + 用户的回答
    updated_content = re.sub(
        r'(## CONSULT \w+ \(Step \d+\) — STATUS:) AWAITING\n+(### 你的回填.*?)\n*$',
        rf'\1 READY\n\n\2\n\n{answer.answer}',
        content,
        flags=re.MULTILINE | re.DOTALL
    )
    
    human_review.write_text(updated_content)
    
    # 恢复项目运行
    env = os.environ.copy()
    env["PATH"] = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    cmd = ["/usr/bin/bash", str(LAUNCH_SCRIPT), "resume", base_name]
    subprocess.run(cmd, env=env)
    
    return {
        "status": "ok",
        "message": "Consultation answer submitted and project resumed"
    }
```

### 3. 前端组件结构

```
components/
├── ConsultationPanel.vue （已有，需增强）
│   ├── ConsultationRequest （新增）
│   ├── DeepThinkPrompt （新增）
│   └── AnswerInput （增强）
└── MarkdownRenderer.vue （复用）
```

### 4. 用户体验流程

#### 理想流程：
1. 用户在 tfisher.de 看到项目状态为 `CONSULT(0)`
2. 点击项目 → 进入「人工咨询」标签页
3. 看到三个区域：
   - **咨询请求**（显示系统需要什么决策）
   - **Deep Think 提示词**（一键复制，打开 Gemini）
   - **回答输入框**（粘贴 Gemini 的回答）
4. 用户复制提示词 → 打开 Gemini Deep Think → 获得回答 → 粘贴回来
5. 点击「提交并恢复运行」
6. 系统自动写入 `human_review.md` 并恢复项目
7. 用户回到项目列表，看到项目状态变为 `RUNNING`

## 实施优先级

### P0（必须）
- [ ] 显示咨询请求完整内容
- [ ] 提供回答输入框和提交功能
- [ ] 后端 API：提交回答并恢复运行

### P1（重要）
- [ ] 显示 Deep Think 提示词
- [ ] 一键复制提示词功能
- [ ] 后端 API：获取咨询提示词
- [ ] 自动打开 Gemini 链接

### P2（增强）
- [ ] 回答预览功能
- [ ] Markdown 渲染支持
- [ ] 提交前确认对话框
- [ ] 历史咨询记录查看

## 技术细节

### 后端需要返回的数据结构

```json
{
  "consultation": {
    "gate": "preflight",
    "step": 0,
    "title": "启动前 seed：题目解读与候选方法",
    "created": "2026-06-21 02:27:57",
    "content": "# 咨询请求完整 Markdown 内容...",
    "status": "AWAITING",
    "key_files": [
      "problem/problem_brief.md",
      "problem/data_inventory.md"
    ]
  },
  "prompt": {
    "text": "向 Gemini Deep Think 的提示词...",
    "gemini_url": "https://gemini.google.com"
  },
  "answer_placeholder": "### 你的回填（preflight）：\n\n"
}
```

### 前端状态管理

```javascript
const consultationState = {
  request: null,        // 咨询请求对象
  prompt: '',           // Deep Think 提示词
  answer: '',           // 用户输入的回答
  isSubmitting: false,  // 提交中状态
  error: null           // 错误信息
}
```

## 相关文件

- `/home/tfisher/paper_factory/ongoing/cumcm_2025_a/consultation/preflight_request.md` - 咨询请求
- `/home/tfisher/paper_factory/ongoing/cumcm_2025_a/human_review.md` - 人工回答
- `/home/tfisher/paper_factory/ongoing/cumcm_2025_a/GEMINI_DEEPTHINK_PROMPT.md` - 提示词模板
- `web/frontend/src/components/ConsultationPanel.vue` - 前端组件
- `web/backend/app.py` - 后端 API

## 待办事项检查清单

- [ ] 阅读并理解现有 `ConsultationPanel.vue` 代码
- [ ] 设计新的 API 端点（获取提示词、提交回答）
- [ ] 实现后端 API
- [ ] 增强前端咨询面板
- [ ] 添加 Markdown 渲染支持
- [ ] 实现一键复制功能
- [ ] 测试完整流程
- [ ] 更新文档

---

**创建时间**：2026-06-21  
**项目**：cumcm_2025_a  
**优先级**：P0（项目运行被阻塞，急需）

**注意**：当前项目已在 preflight 咨询点等待，这些改进完成前，用户需要通过命令行手动完成咨询流程。
