# A1/A2/A3 验证策略分析

> **核心问题**: 是否需要运行 Codex 进行 A1/A2/A3 验证？  
> **答案**: **不需要** — 轻量级方案更快、更精确

---

## 🎯 验证目标对比

| 验证项 | 需要验证什么 | 是否需要完整论文 | 推荐方案 |
|--------|-------------|-----------------|---------|
| **A1** 符号覆盖率 | Step 10 门禁逻辑是否正确触发 | ❌ 不需要 | 手工构造测试用例 |
| **A2** ABSTRACT | Step 9/14 占位符是否正确转义 | ❌ 不需要 | 直接编译测试 |
| **A3** 假设数量 | Step 4 门禁逻辑是否正确触发 | ❌ 不需要 | 手工构造假设表 |

---

## 📊 方案对比

### 方案 1：完整运行（运行 Codex）

**执行方式**:
```bash
./launch_agents.sh new verify_symbols_pass cumcm_2024_b.pdf
./launch_agents.sh new verify_symbols_fail cumcm_2024_a.pdf
```

**优点**:
- ✅ 真实场景验证
- ✅ 端到端测试

**缺点**:
- ❌ 时间成本高（2-3 小时 × 2 = 4-6 小时）
- ❌ 资源消耗大（需要 Codex/Claude API）
- ❌ 干扰因素多（Step 1-9 的随机性可能影响结果）
- ❌ 验证目标不明确（是测门禁还是测论文质量？）

**适用场景**:
- 回归测试（验证整体系统无退化）
- 端到端集成测试
- **不适合**单元测试门禁逻辑

---

### 方案 2：轻量级验证（手工构造，推荐）

**执行方式**:
```bash
./scripts/verify_p0_lightweight.sh
```

**优点**:
- ✅ 快速（5-10 分钟）
- ✅ 精确（直接测试门禁逻辑，无干扰）
- ✅ 可控（手工构造边界用例）
- ✅ 可重复（无随机性）
- ✅ 无资源消耗（本地运行）

**缺点**:
- ❌ 不验证完整流程（但这不是目标）

**适用场景**:
- ✅ **单元测试**（本次验证的目标）
- ✅ 门禁逻辑验证
- ✅ 快速迭代开发

---

## 🔬 轻量级方案详细设计

### A1. 符号覆盖率验证

**测试用例 A1.1**（预期 PASS）:
```latex
% test_paper.tex
Model uses: $\alpha$, $\beta$, $\gamma$, $x$, $y$.
```

```markdown
# symbol_table.md
| $\alpha$ | Parameter alpha | - |
| $\beta$ | Parameter beta | - |
| $\gamma$ | Parameter gamma | - |
| $x$ | Variable x | m |
| $y$ | Variable y | m |
```

**预期结果**: 覆盖率 = 5/5 = 1.0 → PASS

**测试用例 A1.2**（预期 FAIL）:
```latex
% test_paper.tex
Model uses: $\alpha$, $\beta$, $\gamma$, $\delta$, $\epsilon$,
$\zeta$, $\eta$, $\theta$, $x_1$, $x_2$, $y_1$, $y_2$, $z$.
```

```markdown
# symbol_table.md（仅 5 个）
| $\alpha$ | ... |
| $\beta$ | ... |
| $\gamma$ | ... |
| $x_1$ | ... |
| $y_1$ | ... |
```

**预期结果**: 覆盖率 = 5/13 = 0.38 → FAIL

---

### A2. ABSTRACT_PLACEHOLDER 验证

**测试用例 A2.1**（Step 9，预期成功）:
```latex
\begin{abstract}
\detokenize{ABSTRACT_PLACEHOLDER}
\end{abstract}
```

**运行**: `compile_paper.sh`

**预期结果**: 编译成功，PDF 生成

**测试用例 A2.2**（裸占位符，预期失败）:
```latex
\begin{abstract}
ABSTRACT_PLACEHOLDER
\end{abstract}
```

**预期结果**: 编译失败（Missing $ 错误）

---

### A3. PROTECTED 假设数量验证

**测试用例 A3.1**（≥ 4，预期 PASS）:
```markdown
| A1 | ... | CONFIRMED | PROTECTED |
| A2 | ... | CONFIRMED | PROTECTED |
| A3 | ... | CONFIRMED | PROTECTED |
| A4 | ... | CONFIRMED | PROTECTED |
```

**检查**: `grep -c "PROTECTED"` → 4 → ✅ 合格

**测试用例 A3.2**（3 个，预期 WARNING）:
```markdown
| A1 | ... | CONFIRMED | PROTECTED |
| A2 | ... | CONFIRMED | PROTECTED |
| A3 | ... | CONFIRMED | PROTECTED |
```

**检查**: `grep -c "PROTECTED"` → 3 → ⚠️ 需补充

**测试用例 A3.3**（1 个，预期 FAIL）:
```markdown
| A1 | ... | CONFIRMED | PROTECTED |
```

**检查**: `grep -c "PROTECTED"` → 1 → ❌ 不合格

---

## 🎯 推荐方案

### 当前阶段（P0 验证）— 使用轻量级方案

**理由**:
1. **验证目标明确**: 测试门禁逻辑，不是测论文质量
2. **效率优先**: 5-10 分钟 vs 4-6 小时
3. **精确度高**: 手工构造边界用例，无干扰因素
4. **资源节约**: 无需 API 调用

**执行**:
```bash
./scripts/verify_p0_lightweight.sh
```

---

### 后续阶段（回归验证）— 使用完整运行

**时机**: C1 回归验证 + C2 消融对比

**理由**:
- 验证整体系统无退化
- 端到端集成测试
- 与 baseline 对比

**执行**:
```bash
./launch_agents.sh new verify_regression_baseline cumcm_2024_b.pdf
```

---

## 📊 时间成本对比

| 方案 | A1 | A2 | A3 | 总计 | Codex 调用 |
|------|----|----|----|----|----------|
| **完整运行** | 2-3h | 2-3h | 2-3h | 6-9h | 需要 |
| **轻量级** | 2min | 1min | 1min | 5min | 不需要 |
| **节约** | 120x | 180x | 180x | **108x** | 100% |

---

## ✅ 结论

**对于 A1/A2/A3 验证，不需要运行 Codex**：

1. **验证目标**: 测试门禁逻辑，不是测论文质量
2. **推荐方案**: 轻量级验证（手工构造测试用例）
3. **时间成本**: 5-10 分钟 vs 6-9 小时（节约 108x）
4. **精确度**: 更高（无干扰因素）
5. **可重复性**: 更好（无随机性）

**完整运行（含 Codex）适用于**:
- 回归测试（验证整体无退化）
- 端到端集成测试
- 与 baseline 对比分析

**当前阶段推荐**:
```bash
# 立即执行轻量级验证（5-10 分钟）
./scripts/verify_p0_lightweight.sh

# 后续再执行完整回归验证（6-9 小时，可选）
```

---

**文档版本**: v1.0  
**创建日期**: 2026-06-14  
**推荐执行**: 轻量级方案优先
