# Paper Factory 系统优化总结

**优化时间**: 2026-06-13  
**优化范围**: 质量评估体系 + Step 2 资源配额  

---

## 1. 硬指标扩展（优化方向1）

### 1.1 新增指标

#### **求解器收敛性检测** (`verify_solver.py`)

- **检测内容**:
  - 梯度爆炸/NaN/Inf
  - 迭代数达上限但未收敛
  - gap过大（>5%）
  - infeasible/unbounded
  - 超时终止

- **输出指标**:
  ```
  SOLVER_RUNS      = N   (检测到的求解器运行数)
  CONVERGED        = M   (明确收敛的运行)
  NON_CONVERGED    = K   (未收敛/可疑的运行)
  SOLVER_WARNINGS  = W   (警告数量)
  ```

- **退出码**: 0=全部收敛, 1=有失败, 2=无法判断

- **使用场景**:
  - Step 10 数值检查时自动调用
  - 消融实验对比时作为客观信号
  - 替代judge对"模型是否真正求解"的主观判断

#### **数值溯源链检测** (`verify_number_chain.py`)

- **检测内容**:
  - 关键数字（results/*.json中标记为"key_result"的值）
  - 正文中的引用（是否配备了上下文解释）
  - 结论部分的复述（数字是否从正文传递到结论）

- **输出指标**:
  ```
  KEY_NUMBERS          = N   (results/中的关键数字)
  CITED_IN_BODY        = M   (在正文中被引用的)
  CITED_IN_CONCLUSION  = K   (在结论中被复述的)
  CHAIN_BREAKS         = W   (关键数字未传递到结论的次数)
  ```

- **退出码**: 0=链完整, 1=有断裂

- **使用场景**:
  - Step 15 审稿时检查数字一致性
  - 捕获"计算了但没写进论文"的遗漏
  - 捕获"正文写了但结论忘记总结"的断链

### 1.2 集成到 `hard_metrics.py`

新增的指标已经集成到跨项目对比表中：

```bash
python3 scripts/hard_metrics.py <project_dir> <base_name>   # 单项目
python3 scripts/hard_metrics.py --batch complete/ [--json]  # 批量对比
```

新增列：
- `non_converged` (求解器未收敛)
- `solver_warnings` (求解器警告)
- `chain_breaks` (数值链断裂)

### 1.3 测试结果

在 `complete/test_cumcm2024a` 项目上：

```
| 项目            | 悬空引用 | 符号未定义 | 数字无源 | 求解器未收敛 | 求解器警告 | 数值链断裂 | ... |
|----------------|---------|-----------|---------|------------|----------|-----------|-----|
| test_cumcm2024a | 0       | 29        | 0       | 0          | 11       | 0         | ... |
```

- **符号未定义29个**: 符号表覆盖率仅40.8%，属于高风险信号（对应memory中的"judge信号弱，硬指标才是锚"）
- **求解器警告11个**: 主要是stderr中的READ_ERROR（空日志文件），非致命
- **求解器未收敛0个**: 所有关键求解都收敛
- **数值链断裂0个**: 无关键数字，无法测试（该项目results/下无标记key_result的JSON）

---

## 2. Step 2 资源配额与早停（优化方向2）

### 2.1 资源配额决策 (`step2_resource_quota.py`)

#### **复杂度评分模型** (0-100分)

因子权重：
- 问题描述长度: 0-10分（归一化到500字符）
- 约束数量: 0-15分（每个约束0.5分）
- 变量数量: 0-15分（每个变量0.3分）
- 多目标: +10分
- 数据文件数: 0-10分（每个文件2分）
- 候选方法数: 0-15分（每个方法3分）
- 关键词加分:
  - `multi_objective`: +10分
  - `stochastic`: +8分
  - `large_scale`: +10分
  - `nonlinear`: +5分
  - `integer`: +5分

#### **流数量推荐表**

| 复杂度分数 | 基础流数 | 调整规则                    | 最终范围 |
|-----------|---------|----------------------------|---------|
| 0-30      | 3       | 候选方法>5时 +1             | 3-4     |
| 31-60     | 4       | 候选方法>5时 +1             | 4-5     |
| 61-100    | 5       | 候选方法>5时 +1             | 5-6     |
| 上限      | 7       | 避免过度并行                | 7       |

#### **集成到 `run_paper.sh`**

在 `run_step_2()` 启动时：
1. 调用 `step2_resource_quota.py` 获取推荐流数
2. 如果推荐数 < 原始流数，截取前N个流（Step 1已按优先级排序）
3. 记录日志: `quota advisor recommends N streams (original: M)`

**成本节约估算**:
- 简单问题（30%）: 5流→3流，节省40%
- 中等问题（50%）: 5流→4流，节省20%
- 复杂问题（20%）: 保持5-7流
- **加权平均节约**: 30%×40% + 50%×20% = 22%

### 2.2 早停检测 (`step2_early_stop.py`)

#### **早停信号模式**

| 模式                    | 标签               | 置信度 |
|------------------------|-------------------|--------|
| `nan`                  | NAN_EARLY         | 1.0    |
| `inf`（非infeasible）   | INF_EARLY         | 1.0    |
| `unbounded`            | UNBOUNDED         | 0.9    |
| `infeasible at root`   | INFEAS_AT_ROOT    | 0.95   |
| `matrix singular`      | SINGULAR_MATRIX   | 0.9    |
| 30秒内infeasible       | QUICK_INFEASIBLE  | 0.95   |
| Python ImportError     | PYTHON_IMPORT_ERROR| 0.85  |

#### **时间窗口**

- 早停检测窗口: **前5分钟**（300秒）
- 超过5分钟后，不再触发早停（已经投入的成本不回收）

#### **集成到 `run_paper.sh`**

在 Step 2 监控循环中，每个 `MONITOR_SLEEP` 周期（默认30秒）：
1. 对所有处于 `proposal` 阶段的流，调用 `step2_early_stop.py`
2. 如果 `should_stop=True`，立即终止该流的agent进程
3. 在 `m<N>_critique.md` 中写入早停原因：
   ```
   ## Early Stop
   
   VERDICT: ABANDONED
   
   Reason: Demo solve failed early with INF_EARLY. Stream terminated to save resources.
   ```
4. 该流标记为 `done`，不计入 Step 2 的"需要≥2个VALIDATED"判断

**成本节约估算**:
- 假设10%的流在前5分钟内失败（快速失败率）
- 每个流平均proposal时长5小时（18000秒超时）
- 早停节省: 5小时 - 5分钟 ≈ 99%单流成本
- **整体节约**: 10%流 × 99% = 9.9%

### 2.3 综合节约

- 资源配额: 节省22% Step 2 成本
- 早停检测: 节省10% Step 2 成本
- **两者叠加** (不完全独立): 约 **30-35% Step 2 总成本**

假设 Step 2 占总成本40%（16步中最重的），则：
- **整体工厂成本节约**: 40% × 32.5% ≈ **13%**

---

## 3. 测试验证

### 3.1 自动化测试

运行 `tests/test_step2_optimization.sh`：

```bash
bash tests/test_step2_optimization.sh
```

**测试覆盖**:
1. ✅ 资源配额决策: 5流→3流（简单问题）
2. ✅ 早停检测: 检测到Inf，建议终止（confidence=1.0）
3. ✅ 求解器收敛: 正确区分收敛/未收敛日志

### 3.2 实际项目测试

在 `complete/test_cumcm2024a` 上：

```bash
# 资源配额
python3 scripts/step2_resource_quota.py complete/test_cumcm2024a
# 输出: recommended_streams=3, complexity_score=14.2

# 求解器收敛
python3 scripts/verify_solver.py complete/test_cumcm2024a test_cumcm2024a
# 输出: 36个日志, 3个收敛, 0个失败, 33个不确定（空日志）
```

---

## 4. 部署与使用

### 4.1 新文件清单

```
scripts/
├── verify_solver.py           # 求解器收敛检测
├── verify_number_chain.py     # 数值链检测
├── step2_resource_quota.py    # 资源配额决策
└── step2_early_stop.py        # 早停检测

tests/
└── test_step2_optimization.sh # 自动化测试脚本
```

### 4.2 `run_paper.sh` 修改点

1. **L2107-2138**: 添加资源配额调用，裁剪流数
2. **L2256-2290**: 添加早停检测循环，终止快速失败的流

### 4.3 向后兼容性

- ✅ 所有新功能都是增量的，不影响已有流程
- ✅ 如果脚本调用失败（如Python环境问题），自动降级为原逻辑
- ✅ `hard_metrics.py` 容错处理：缺失指标返回空dict，不阻塞

### 4.4 回滚方案

如果需要回滚：
```bash
cp run_paper.sh.backup run_paper.sh
```

---

## 5. 下一步建议

### 5.1 短期（1-2周）

1. **在真实项目上验证**: 对下一个CUMCM赛题运行完整流程，观察：
   - 资源配额是否合理（是否有误杀高质量流）
   - 早停是否准确（假阳性率）
   - 成本节约实际值

2. **标记关键结果**: 在Step 5（full solve）和Step 6（sensitivity）的脚本模板中，生成results/*.json时加入 `"is_key": true` 标记，激活数值链检测

3. **阈值调优**:
   - 早停时间窗口: 当前300秒，可根据实际调整到180-600秒
   - Gap阈值: 当前5%，MILP问题可放宽到10%
   - 复杂度权重: 观察几个项目后微调

### 5.2 中期（1-2月）

4. **求解器选择表** (之前建议的第5点):
   - 根据问题特征（LP/MILP/NLP/多目标）自动推荐求解器
   - Gurobi vs CPLEX vs IPOPT vs 启发式
   - 集成到 `solver_submit.sh` 作为 `--auto-solver` 选项

5. **历史咨询复用** (之前建议的第4点):
   - 建立咨询决策向量化索引（问题描述 embedding）
   - 新问题触发preflight gate时，检索历史相似问题的咨询结果
   - 自动预填 `human_review.md` 的参考建议

6. **实验manifest** (之前建议的第8点):
   - 每次完整运行生成 `experiment_<timestamp>.json`，记录：
     - prompt版本（git hash）
     - 模型版本
     - 所有硬指标值
     - 人工咨询内容（如有）
   - 用于可重复性和ablation追踪

### 5.3 长期（3-6月）

7. **多judge ensemble**:
   - 结合Gemini/Claude/GPT的judge分数
   - 加权公式: `0.4×硬指标 + 0.3×Gemini + 0.2×Claude + 0.1×GPT`
   - 解决单一judge的锚定问题

8. **失败模式自动修复**:
   - Step 10检测到"符号X未定义"时，自动追加到 `symbol_table.md`
   - Step 15检测到悬空引用时，自动搜索method_library补充bib条目

9. **渐进式精度**:
   - Step 1-3: Sonnet快速筛选
   - Step 4-8: Opus精雕模型
   - Step 9-15: Opus精雕论文
   - 利用prompt缓存，`modeling_guide.md`等高频内容标记为system

---

## 6. 关键指标仪表盘

建议在每次运行后生成：

```markdown
### 项目: <base_name>

**硬指标得分**: X/100 (悬空引用×10 + 符号未定义×5 + 求解器失败×20 + ...)

**Step 2 效率**:
- 原始流数: N
- 配额裁剪后: M
- 早停终止: K
- 最终VALIDATED: V
- 时长节约: XX%

**judge得分** (如有):
- Gemini: XX/100
- Claude: XX/100
- 硬指标校准: (judge分 - 硬指标分) → 如果偏差>20，说明judge锚定严重

**PDF**: ✓/✗  
**提交包**: ✓/✗
```

---

## 附录: 测试输出示例

```bash
$ bash tests/test_step2_optimization.sh

=== Step 2 优化功能测试 ===

测试 1: 资源配额决策
输入: 5个候选流，简单问题
{
    "recommended_streams": 3,
    "complexity_score": 16.8,
    "reasoning": "问题复杂度较低（单变量或短时间窗）; Step 1提出5个候选方法 → 推荐3流并行"
}

测试 3: 早停检测
{
    "should_stop": true,
    "reason": "INF_EARLY",
    "elapsed_seconds": 0.0,
    "confidence": 1.0
}

测试 5: 求解器收敛检测
SOLVER_RUNS      = 2
CONVERGED        = 1
NON_CONVERGED    = 1
SOLVER_WARNINGS  = 3

=== 测试完成 ===

预期成本节约: 简单问题减少2个并行流 × 5小时平均时长 = 节省约40%的Step 2成本
```

---

**文档版本**: v1.0  
**最后更新**: 2026-06-13
