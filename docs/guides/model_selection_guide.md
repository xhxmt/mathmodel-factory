# 模型选择与配置指南

本文档说明如何为 Modeling Factory 项目配置和选择执行模型。

## 概述

Modeling Factory 支持逐步选择模型(per-step model selection),即为不同的工作流步骤分配不同的 AI 模型。这允许:

- **成本优化**: 简单步骤使用快速模型,复杂步骤使用强力模型
- **质量控制**: 关键步骤(评委打分、建模设计)使用最佳模型
- **并发执行**: 不同步骤可同时调用不同后端,避免单一 API 限流

## 配置文件

### 1. `web/model_registry.json` — 可用模型注册表

定义系统中所有可用的模型及其连接信息:

```json
{
  "models": [
    {
      "id": "claude",               // 唯一标识符
      "label": "Claude (默认 CLI)",  // UI 显示名称
      "backend": "claude",          // 执行后端: claude | codex | agy | openai | gemini
      "model": "",                  // 模型名称(空表示使用后端默认)
      "effort": "max",              // 思考努力程度(claude 专用)
      "base_url": "",               // API 基础 URL(自定义 API 端点)
      "key_env": "",                // API key 环境变量名(空表示使用默认认证)
      "enabled": true,              // 是否启用
      "builtin": true               // 是否为内置模型(影响 UI 显示顺序)
    }
  ]
}
```

**内置模型**(builtin=true):
- `claude`: 本地 Claude CLI(默认)
- `codex-gpt55`: Codex CLI 调用 GPT-5.5
- `agy-gemini`: Antigravity SDK 调用 Gemini 3.1 Pro

**外部 API 模型**(builtin=false):
- `deepseek-chat` / `deepseek-reasoner`: DeepSeek API(适合评委打分)
- `gemini-api`: Google Gemini API
- `qwen-max`: 阿里云 Qwen Max(需配置 DASHSCOPE_API_KEY)

### 2. `ongoing/<project>/model_config.json` — 项目级模型分配

为每个步骤分配具体模型(可选文件,不存在时使用全局默认):

```json
{
  "_default": {
    "step_1": {"primary": "codex-gpt55"},
    "step_2": {"primary": "codex-gpt55"},
    "step_4": {"primary": "agy-gemini"},
    "step_7": {"primary": "gemini-api", "fallback": "claude"},
    "step_11": {"primary": "deepseek-chat", "fallback": "codex-gpt55"},
    "step_13": {"primary": "deepseek-reasoner", "fallback": "gemini-api"}
  }
}
```

**字段说明**:
- `primary`: 优先使用的模型 ID(来自 `model_registry.json`)
- `fallback`: 主模型失败时的后备模型(可选)
- 未指定的步骤使用 `run_paper.sh` 的默认调度逻辑

## 典型配置场景

### 场景 1: 全 Claude 默认(最简单)

不创建 `model_config.json`,所有步骤使用 `claude` CLI 默认模型。

**优点**: 零配置,开箱即用
**缺点**: 无法利用专用模型优势(如 DeepSeek 评委)

### 场景 2: 评委步骤使用外部 API

```json
{
  "_default": {
    "step_7": {"primary": "deepseek-chat"},
    "step_11": {"primary": "deepseek-chat"},
    "step_13": {"primary": "deepseek-reasoner", "fallback": "deepseek-chat"}
  }
}
```

Step 7(模型评价)、Step 11(constructive review)、Step 13(评委打分) 使用 DeepSeek,其余步骤使用默认 Claude。

**前置要求**: 在 `.env` 中配置 `DEEPSEEK_API_KEY=sk-...`

### 场景 3: 多模型混合(质量最优)

```json
{
  "_default": {
    "step_1": {"primary": "agy-gemini"},           // 背景研究: Gemini 3.1 Pro 网页检索
    "step_4": {"primary": "codex-gpt55"},         // 模型构建: GPT-5.5 高阶建模
    "step_5": {"primary": "codex-gpt55"},         // 求解编排: GPT-5.5 代码生成
    "step_7": {"primary": "gemini-api"},          // 模型评价: Gemini 2.5 Pro API
    "step_11": {"primary": "deepseek-chat"},      // 建设性审稿: DeepSeek
    "step_13": {"primary": "deepseek-reasoner", "fallback": "gemini-api"}  // 评委打分: DeepSeek Reasoner
  }
}
```

**前置要求**:
- `GEMINI_API_KEY=...`
- `DEEPSEEK_API_KEY=...`

### 场景 4: 成本优化(降低 API 调用)

```json
{
  "_default": {
    "step_7": {"primary": "deepseek-chat"},
    "step_13": {"primary": "deepseek-chat"}
  }
}
```

仅在评委相关步骤使用外部 API,其余使用本地 Claude CLI(假设有本地 Claude 订阅)。

## 添加新模型

### 步骤 1: 编辑 `web/model_registry.json`

添加新的模型条目:

```json
{
  "id": "my-custom-api",
  "label": "My Custom Model",
  "backend": "openai",              // 兼容 OpenAI API 格式
  "model": "my-model-name",
  "effort": "",
  "base_url": "https://api.myprovider.com/v1",
  "key_env": "MY_PROVIDER_API_KEY",
  "enabled": true,
  "builtin": false
}
```

### 步骤 2: 在 `.env` 中配置 API key

```bash
MY_PROVIDER_API_KEY=your-api-key-here
```

### 步骤 3: 在项目中启用

创建或编辑 `ongoing/<project>/model_config.json`:

```json
{
  "_default": {
    "step_13": {"primary": "my-custom-api"}
  }
}
```

### 步骤 4: 测试

```bash
./launch_agents.sh run <project>
```

检查日志确认新模型被正确调用。

## 成本估算

不同模型的相对成本(以 Step 13 评委打分为例):

| 模型 | 输入成本 | 输出成本 | 典型 Step 13 成本 |
|------|---------|---------|------------------|
| Claude Opus 4.6 (本地订阅) | 订阅制 | 订阅制 | ~$0 (已订阅) |
| DeepSeek Chat | $0.14/M | $0.28/M | ~$0.05 |
| DeepSeek Reasoner | $0.55/M | $2.19/M | ~$0.20 |
| Gemini 2.5 Pro | $1.25/M | $5.00/M | ~$0.40 |
| GPT-5.5 (通过 Codex) | 依赖 Codex 定价 | - | 变动 |

**建议**: 如果有 Claude Pro 订阅,优先使用 `claude`; 如果需要外部 API,DeepSeek 性价比最高。

## 模型选择原则

### 按步骤类型选择

| 步骤类型 | 推荐模型 | 原因 |
|---------|---------|------|
| Step 1 (背景研究) | `agy-gemini` | Gemini 有网页检索能力 |
| Step 2 (并行建模提案) | `codex-gpt55` | 高并发需求,GPT 快速响应 |
| Step 4 (模型构建) | `codex-gpt55` / `claude` | 需要深度数学建模 |
| Step 5 (求解编排) | `codex-gpt55` | 代码生成质量高 |
| Step 7 (模型评价) | `deepseek-chat` / `gemini-api` | 需要批判性思维 |
| Step 11 (constructive review) | `deepseek-chat` | 审稿需要细致分析 |
| Step 13 (评委打分) | `deepseek-reasoner` | 需要 reasoning trace |
| 其他步骤 | `claude` | 通用能力平衡 |

### 成本 vs 质量权衡

**高质量方案**(预算充足):
- Step 1/4/5: `codex-gpt55` 或 `agy-gemini`
- Step 7/11/13: 外部 API(DeepSeek / Gemini)
- 其他: `claude`

**平衡方案**(推荐):
- Step 7/13: `deepseek-chat` / `deepseek-reasoner`
- 其他: `claude`

**成本优化方案**(预算紧张):
- 全部使用 `claude`(假设已订阅 Claude Pro)
- 或全部使用 `codex` 默认模型

## 故障排查

### 问题: 模型调用失败

**症状**: 日志显示 `run_paper.sh` 某步骤失败,错误提示 API 连接问题

**排查步骤**:
1. 检查 `.env` 中对应的 API key 是否正确
2. 检查 `model_registry.json` 中 `base_url` 是否正确
3. 手动测试 API 连接:
   ```bash
   curl -H "Authorization: Bearer $DEEPSEEK_API_KEY" \
        https://api.deepseek.com/v1/models
   ```
4. 检查 `enabled: true` 是否设置
5. 查看 `ongoing/<project>/logs/step_X_*.log` 的详细错误信息

### 问题: fallback 模型未生效

**原因**: `run_paper.sh` 的 `dispatch_step` 函数目前仅支持单一模型调度,fallback 需要额外逻辑

**临时解决方案**: 在 `model_config.json` 中直接指定可靠的主模型,移除 fallback 字段

**永久方案**: 等待 `run_paper.sh` 集成 fallback 重试逻辑(roadmap 中)

### 问题: model_config.json 不生效

**检查**:
1. 文件是否在 `ongoing/<project>/model_config.json`(不是仓库根目录)
2. JSON 格式是否正确(使用 `jq . model_config.json` 验证)
3. 模型 ID 是否在 `model_registry.json` 中注册
4. `run_paper.sh` 是否支持该步骤的模型覆盖(当前仅 Step 7/11/13 完全支持)

## Web Dashboard 集成

在 Web Dashboard 中:
- **新建项目时**: 可通过"模型配置"选项卡选择各步骤模型
- **运行中项目**: 可在项目详情页查看当前模型分配
- **模型管理**: 在设置页面启用/禁用模型,添加新模型

详见 `web/README.md`。

## 后续计划

- [ ] 自动成本预估(创建项目前预测总成本)
- [ ] 模型性能监控(记录每步骤的 tokens 消耗和耗时)
- [ ] 智能模型推荐(基于问题类型推荐最优模型组合)
- [ ] Fallback 链支持(primary → fallback1 → fallback2)
- [ ] 模型预算控制(设置项目总 token 上限,超限时自动降级模型)

## 参考资料

- `web/model_registry.json` — 全局模型注册表
- `web/backend/app.py` — 模型调度后端实现
- `run_paper.sh` § `dispatch_step` — 步骤调度逻辑
- `CLAUDE.md` § Per-step model selection subsystem — 架构说明
