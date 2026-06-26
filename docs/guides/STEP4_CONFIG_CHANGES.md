# Step 4 配置修改完成报告

## 已实施的修改

### 1. ✅ 将 Step 4 的执行引擎从 Agy 改为 Claude/Codex

**修改位置**: `/home/tfisher/paper_factory/run_paper.sh` - `run_step_4()` 函数

**修改内容**:
```bash
# 旧配置（已废弃）
log "   Step 4: full model construction (Agy → Claude fallback)"
dispatch_step step4_model_construction.txt 14400 3600 run_agy_then_claude || true

# 新配置（已生效）
log "   Step 4: full model construction (Claude → Codex fallback, Agy disabled)"
dispatch_step step4_model_construction.txt 14400 3600 run_claude_then_codex || true
```

**效果**:
- ✅ Step 4 现在优先使用 **Claude Opus**
- ✅ 如果 Claude 失败，自动 fallback 到 **Codex (GPT-5.5)**
- ✅ **完全禁用 Agy**，避免快速但可能有缺陷的建模

### 2. ✅ Step 4 的咨询机制已存在且可配置

**咨询点位置**: `run_paper.sh` line 3101
```bash
if (( NEXT == 4 )); then
    maybe_consult step4 4 "建模定型前：确认 model.md 的建模路线（贴前沿模型的建模方案）"
fi
```

**咨询启用方式有三种**:

#### 方式 A：环境变量（全局启用）
```bash
export CONSULT_ENABLE=1
./launch_agents.sh new <base> "/path/to/problem.pdf"
```
- 启用所有咨询点（preflight、step4、dynamic）

#### 方式 B：项目级配置（推荐，精确控制）
```bash
# 创建项目时
./launch_agents.sh new --consult <base> "/path/to/problem.pdf"

# 这会创建 ongoing/<base>/consultation/enabled 文件
# 内容：preflight step4
```

#### 方式 C：手动创建配置文件
```bash
mkdir -p ongoing/<base>/consultation
echo "preflight step4" > ongoing/<base>/consultation/enabled
```

## 如何确保 Step 4 咨询是强制性的

### 标准流程（推荐）

**启动新项目时**:
```bash
cd /home/tfisher/paper_factory

# 使用 --consult 标志启动（这会自动启用 preflight 和 step4）
./launch_agents.sh new --consult cumcm_2025_a_v2 "/path/to/problem.pdf"
```

**consultation/enabled 文件内容**:
```
preflight step4
```

### 咨询工作流

#### Step 4 到达时的行为

1. **Runner 检测到 Step 4**
2. **调用 `maybe_consult step4`**
3. **生成咨询请求文件**: `consultation/step4_request.md`
4. **在 `human_review.md` 中创建填写区**:
```markdown
## CONSULT step4 (Step 4) — STATUS: AWAITING

### 你的回填（step4）：

（在这里粘贴 Gemini Deep Think 或 GPT Pro 的分析）
```
5. **Runner 干净退出**（exit 0），不继续执行
6. **等待人工输入**

#### 人工操作步骤

1. **阅读咨询请求**:
```bash
cat ongoing/<base>/consultation/step4_request.md
```

2. **使用 Gemini Deep Think 分析**（或 GPT Pro）

3. **填写回答到 `human_review.md`**:
```markdown
## CONSULT step4 (Step 4) — STATUS: READY

### 你的回填（step4）：

（粘贴 Gemini 的分析，包括建模方案评审、风险评估、改进建议等）
```

4. **恢复运行**:
```bash
./launch_agents.sh resume <base>
```

#### Runner 恢复后的行为

1. **检测到 `STATUS: READY`**
2. **读取 `human_review.md` 中的人工回答**
3. **继续执行 Step 4**，Claude/Codex 会看到人工提供的建议
4. **完成建模构建**

## 强制性保证

### 已有的保证机制

✅ **咨询点是阻塞的**:
- 如果 `consultation/enabled` 包含 `step4`
- 且 `human_review.md` 中没有 `STATUS: READY`
- Runner 会 **exit 0**（干净退出），不会继续

✅ **不会跳过**:
- 没有超时机制
- 没有"N 小时后自动继续"
- 必须人工标记 `STATUS: READY` 才能继续

✅ **可重复使用**:
- 如果需要重新咨询，删除 `STATUS: READY` 即可

### 当前项目的咨询历史

查看已完成的 cumcm_2025_a 项目:
```bash
cat /home/tfisher/paper_factory/complete/cumcm_2025_a/consultation/enabled
# 输出: preflight step4

ls /home/tfisher/paper_factory/complete/cumcm_2025_a/consultation/
# 输出:
# - enabled
# - preflight_request.md
# - step4_request.md

cat /home/tfisher/paper_factory/complete/cumcm_2025_a/human_review.md
# 可以看到两个咨询点的回答
```

## 针对新项目的建议

基于 cumcm_2025_a 的结果分析，Step 4 咨询应该重点关注：

### 必须审查的建模决策

#### 1. 遮蔽判据设计 ⚠️
- **当前问题**: 26 点采样太少，导致 +6.4% 误差
- **人工审查**: 采样点数量是否足够？（建议 100-300 点）
- **检查方式**: 查看 `model.md` 中的采样策略

#### 2. 多机协同策略 🔴
- **当前问题**: 问题 4 高估 52.2%（16.94 vs 11.13 秒）
- **人工审查**: 
  - 协同优化是否有重复计数？
  - 热启动策略是否合理？
  - 是否需要降维处理？
- **检查方式**: 查看 `models/m1_pso/` 的协同逻辑

#### 3. 多导弹目标函数 🔴 **最关键**
- **当前问题**: 问题 5 使用了错误的目标函数（求和 vs 交集）
- **人工审查**: 
  - ❌ 错误: `objective = sum(Z_M1, Z_M2, Z_M3)` 
  - ✅ 正确: `objective = intersection(I_M1, I_M2, I_M3)`
- **检查方式**: 查看 `models/m3_milp/` 的目标函数定义

### 咨询提示词模板

为 Step 4 准备的人工审查提示词应该包含：

```markdown
# Step 4 建模方案审查

## 背景
已完成 Step 2-3，选定了建模方案。现在需要审查即将在 Step 4 构建的完整模型。

## 需要审查的关键决策

### 1. 遮蔽判据
- 当前设计: [从 model.md 提取]
- 采样点数量: [检查是否足够]
- 是否会有"漏光"问题?

### 2. 多机协同策略（问题 4）
- 决策变量维度: [12 维 / 降维策略]
- 协同优化方法: [联合 PSO / 分层优化]
- 是否会重复计数?

### 3. 多导弹分配（问题 5）
- 目标函数定义: [求和 / 交集]
- 这是最关键的！必须是"所有导弹同时被遮蔽的时间"（交集）
- 不是"各导弹遮蔽时间总和"（求和）

## 参考基准
- 优秀论文问题 4: 11.126 秒
- 优秀论文问题 5: 21.077 秒
- 如果你的方案预测会偏离这些数值 >20%，请说明原因

## 审查后输出格式

✅ 批准 / ⚠️ 有保留地批准 / ❌ 需要重大修改

### 必须修改的点
1. ...
2. ...

### 建议改进的点
1. ...
2. ...
```

## 部署状态

- ✅ `run_paper.sh` 已修改（Step 4 使用 Claude/Codex）
- ✅ 咨询机制已存在且已测试（cumcm_2025_a 使用过）
- ✅ 配置方式已文档化
- ⏳ 下次新项目使用 `--consult` 标志启动即可

## 测试建议

启动一个测试项目验证配置：

```bash
cd /home/tfisher/paper_factory

# 1. 启动带咨询的项目
./launch_agents.sh new --consult test_consult "/path/to/test_problem.pdf"

# 2. 观察 Step 4 是否正确暂停
# 应该看到: "CONSULT[step4]: pausing for human input"

# 3. 检查生成的文件
ls ongoing/test_consult/consultation/
# 应该有: enabled, preflight_request.md, step4_request.md

# 4. 填写 human_review.md 并恢复
vim ongoing/test_consult/human_review.md
# 改 STATUS: AWAITING → STATUS: READY

./launch_agents.sh resume test_consult
# 应该继续执行 Step 4
```

---

**修改完成时间**: 2026-06-21 08:50  
**状态**: ✅ 已部署，待下次项目测试  
**关键改进**: Step 4 不再使用 Agy，咨询机制保持不变但已优化提示词
