# P0 + P1 修复验证实验计划

> **目标**: 验证 8 个修复项的实际效果  
> **方法**: 对照实验 + 边界测试 + 回归验证  
> **预计时间**: 3-5 天（包含完整流程运行）  
> **负责人**: Paper Factory Team  
> **日期**: 2026-06-14

---

## 📋 实验概览

| 实验组 | 目标 | 方法 | 预计时间 | 优先级 |
|--------|------|------|---------|---------|
| **A. P0 核心验证** | 验证 4 个 P0 修复 | 运行 A/B 题 + 边界测试 | 2-3 天 | P0 |
| **B. P1 功能验证** | 验证 4 个 P1 修复 | 单元测试 + 集成测试 | 1 天 | P1 |
| **C. 回归验证** | 确认无副作用 | 重跑 baseline | 0.5 天 | P0 |
| **D. 对比分析** | 量化改进幅度 | 前后数据对比 | 0.5 天 | P2 |

**总计**: 4-5 天（可并行执行 A + B）

---

## 🔬 实验组 A: P0 核心验证

### A1. 符号覆盖率硬门禁验证

**目标**: 验证 Step 10 符号覆盖率检查是否正常工作

**实验设计**:

#### A1.1 正向验证（预期 PASS）
```bash
# 运行 B 题 baseline（已知符号覆盖率良好）
./launch_agents.sh new --no-start verify_p0_symbols_pass \
    "/absolute/path/to/cumcm_2024_b.pdf"
./launch_agents.sh run verify_p0_symbols_pass

# 等待运行到 Step 10
./launch_agents.sh attach verify_p0_symbols_pass
```

**预期结果**:
- Step 10 执行 `verify_symbols.py`
- `code_review.md` §0 显示覆盖率 > 0.7
- Gate 1 verdict: PASS
- 日志包含 "符号覆盖率审计: ✅ PASS"

**成功标准**:
- [x] `code_review.md` 包含 §0 符号覆盖率审计
- [x] 覆盖率数值在 0.7-1.0 之间
- [x] 未定义符号清单为空或很少（< 5 个）

#### A1.2 负向验证（预期 FAIL）
```bash
# 运行 A 题（已知符号覆盖率 0.408）
./launch_agents.sh new --no-start verify_p0_symbols_fail \
    "/absolute/path/to/cumcm_2024_a.pdf"
./launch_agents.sh run verify_p0_symbols_fail
```

**预期结果**:
- Step 10 执行 `verify_symbols.py`
- `code_review.md` §0 显示覆盖率 = 0.408
- Gate 1 verdict: **FAIL** — 符号覆盖率 < 0.5
- 工作流停止在 Step 10，不进入 Step 11

**成功标准**:
- [x] `code_review.md` 显示 "❌ FAIL（质量崩溃，必须回 Step 4）"
- [x] 未定义符号清单包含 ~29 个符号
- [x] `checkpoint.md` 显示 "Last completed step: 10"（未进 Step 11）

#### A1.3 边界验证（覆盖率 0.5-0.7）
创建人工测试项目：
```bash
cd tests/
mkdir -p test_symbol_warning/
# 手工构造 symbol_table.md 覆盖 60% 符号
# 预期: WARNING 但通过
```

---

### A2. ABSTRACT_PLACEHOLDER 转义验证

**目标**: 验证所有步骤正确使用 `\detokenize{ABSTRACT_PLACEHOLDER}`

**实验设计**:

#### A2.1 Step 9 生成验证
```bash
# 从 Step 8 完成的项目继续
cd ongoing/verify_p0_symbols_pass/
../../run_paper.sh --infer-step "$(pwd)"  # 确认在 Step 8

# 手工触发 Step 9
# （或等待自动运行到 Step 9）
```

**验证点**:
```bash
# 检查 Step 9 生成的占位符
grep "ABSTRACT_PLACEHOLDER" *_paper.tex

# 预期输出（2 种合法形式之一）:
# \detokenize{ABSTRACT_PLACEHOLDER}
# 或 ABSTRACT\_PLACEHOLDER（转义下划线）
```

**成功标准**:
- [x] `grep "ABSTRACT_PLACEHOLDER" *_paper.tex` 找到 1 处
- [x] 占位符被 `\detokenize{}` 包裹或下划线被转义
- [x] `compile_paper.sh` 第一次编译成功（无语法错误）

#### A2.2 Step 14 替换验证
```bash
# 等待运行到 Step 14
# 检查替换后的 .tex
grep "ABSTRACT_PLACEHOLDER" *_paper.tex

# 预期输出: 空（已被摘要文本替换）
```

**成功标准**:
- [x] `grep "ABSTRACT_PLACEHOLDER" *_paper.tex` 返回空
- [x] `grep "detokenize" *_paper.tex` 返回空
- [x] PDF 包含完整摘要（非占位符）

---

### A3. PROTECTED 假设数量硬门禁验证

**目标**: 验证 Step 4 假设数量检查是否正常工作

**实验设计**:

#### A3.1 正向验证（≥ 4 个 PROTECTED）
```bash
# 运行 B 题（已知 8 个 PROTECTED）
# 使用 A1.1 的项目
cd ongoing/verify_p0_symbols_pass/
cat assumption_ledger.md | grep PROTECTED | wc -l

# 预期: 6-8 个
```

**成功标准**:
- [x] `assumption_ledger.md` §PROTECTED 假设数量自检 显示 ≥ 4
- [x] 判定: ✅ 合格
- [x] 无 `## Step 4 自检警告` 子节

#### A3.2 边界验证（2-3 个 PROTECTED）
手工构造测试项目：
```bash
cd tests/test_protected_warning/
# 编辑 assumption_ledger.md，只保留 3 个 PROTECTED
# 预期: ⚠️ 警告但继续
```

**成功标准**:
- [x] `assumption_ledger.md` 显示 "⚠️ 需补充"
- [x] 警告信息包含 "PROTECTED 假设仅 3 个，低于建议阈值 4"

#### A3.3 负向验证（< 2 个 PROTECTED）
```bash
# 手工构造 1 个 PROTECTED 的项目
# 预期: ❌ FAIL，Step 4 停止
```

**成功标准**:
- [x] `model.md` 末尾包含 `## Step 4 自检警告`
- [x] 说明 "PROTECTED 假设仅 1 个，未达最低阈值 2"
- [x] Step 4 未生成完整代码（models/ 目录不完整）

---

### A4. 方法库几何方法验证

**目标**: 验证新增的 3 个几何方法是否可被检索和引用

**实验设计**:

#### A4.1 方法库完整性检查
```bash
# 检查 index.json
python3 -c "
import json
with open('method_library/index.json') as f:
    methods = json.load(f)
print(f'方法总数: {len(methods)}')

geom_methods = [m for m in methods if m['domain'] == 'geometry']
print(f'几何方法数: {len(geom_methods)}')
for m in geom_methods:
    print(f\"  - {m['method']}: {m['path']}\")
"

# 预期输出:
# 方法总数: 16
# 几何方法数: 5
#   - Archimedean Spiral: ...
#   - Collision Detection (SAT): ...
#   - Broad-Phase Collision / AABB: ...
#   - Curve Joining: ...
#   - Feasibility Bisection: ...
```

**成功标准**:
- [x] 方法总数 = 16
- [x] 几何方法数 = 5（原有 2 个 + 新增 3 个）
- [x] 3 个新方法文件存在且 ≥ 100 行

#### A4.2 方法检索验证
```bash
# 模拟 Step 0 方法检索
python3 scripts/method_retrieve.py \
    --query "板材龙舟 调头 碰撞检测 最大速度" \
    --top 5

# 预期输出（应包含新方法）:
# 1. broad_phase_collision.md (AABB)
# 2. collision_detection.md (SAT)
# 3. feasibility_bisection.md (二分搜索)
# 4. ...
```

**成功标准**:
- [x] 检索结果包含至少 1 个新增几何方法
- [x] 相关性评分合理（> 0.5）

---

## 🧪 实验组 B: P1 功能验证

### B1. 灵敏度分析最小覆盖验证

**目标**: 验证 Step 6 自检门禁是否正常工作

**实验设计**:

#### B1.1 正向验证（完整灵敏度）
```bash
# 使用 A1.1 的项目，等待运行到 Step 6
cd ongoing/verify_p0_symbols_pass/

# Step 6 完成后检查
wc -l sensitivity_report.md
# 预期: ≥ 150 行

ls figures/sensitivity_*.pdf | wc -l
# 预期: ≥ 2 张图

grep -A 10 "灵敏度分析最小覆盖自检" sensitivity_report.md
# 预期: 判定 ✅ PASS
```

**成功标准**:
- [x] `sensitivity_report.md` ≥ 150 行
- [x] 至少 2 张图（1 tornado + 1 scenario）
- [x] 假设状态升级率 ≥ 60%
- [x] 自检章节显示 "✅ PASS"

#### B1.2 负向验证（敷衍灵敏度）
手工构造不完整的 sensitivity_report.md：
```bash
cd tests/test_sensitivity_warning/
# 只写 100 行，只有 1 张图
# 预期: ⚠️ WARNING
```

**成功标准**:
- [x] 自检章节显示 "⚠️ 不足"
- [x] 列出缺失项（如 "tornado 图缺失"）

---

### B2. 编译失败诊断验证

**目标**: 验证 compile_paper.sh 智能诊断是否工作

**实验设计**:

#### B2.1 Undefined control sequence 错误
```bash
cd tests/test_compile_error/
# 创建测试 .tex 文件
cat > test_paper.tex <<'EOF'
\documentclass{article}
\begin{document}
This is a test.
\abc{undefined command}
\end{document}
EOF

# 运行编译
../../compile_paper.sh "$(pwd)" test
```

**预期输出**:
```
❌ 编译失败：第一次 pdflatex 编译出错

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📋 错误诊断（提取自 test_paper.log）：
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
! Undefined control sequence.
l.4 \abc
        {undefined command}

🔍 诊断：未定义的控制序列（可能缺少宏包或拼写错误）
\abc

💡 完整日志位置: .../logs/compilation/pass1.log
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

**成功标准**:
- [x] 错误输出包含 "Undefined control sequence"
- [x] 显示错误行号和上下文
- [x] 提供诊断建议
- [x] 日志文件路径正确

#### B2.2 Missing $ inserted 错误
```bash
cat > test_paper.tex <<'EOF'
\documentclass{article}
\begin{document}
This is a_test with unescaped underscore.
\end{document}
EOF

../../compile_paper.sh "$(pwd)" test
```

**预期输出**:
```
🔍 诊断：数学模式错误（可能缺少 $ 或 _ 未转义）
```

**成功标准**:
- [x] 诊断提及 "数学模式错误" 或 "下划线未转义"

#### B2.3 成功编译信息
```bash
cat > test_paper.tex <<'EOF'
\documentclass{article}
\begin{document}
This is a correct document.
\end{document}
EOF

../../compile_paper.sh "$(pwd)" test
```

**预期输出**:
```
✅ PDF 已生成: test_paper.pdf (8.5K, 1 页)
```

**成功标准**:
- [x] 显示 PDF 大小和页数
- [x] `logs/compilation/compile.log` 包含编译历史

---

### B3. 章节结构动态化验证

**目标**: 验证 Step 9 能否识别竞赛类型并生成正确章节

**实验设计**:

#### B3.1 CUMCM 模式验证
```bash
# 使用 B 题（CUMCM）
cd ongoing/verify_p0_symbols_pass/

# Step 9 完成后检查
grep -E "\\section\{问题重述\}|\\section\{模型建立\}" *_paper.tex | wc -l
# 预期: ≥ 2（中文章节）

head -50 *_paper.tex | grep -E "ctexart|xeCJK"
# 预期: 使用中文文档类
```

**成功标准**:
- [x] 章节标题为中文
- [x] 使用 `ctexart` 或 `xeCJK`
- [x] 摘要为四段格式（问题理解/方法/结果/亮点）

#### B3.2 MCM 模式验证
```bash
# 创建 MCM 题目测试
cd tests/
mkdir -p test_mcm_format/problem/
cat > test_mcm_format/problem/source.md <<'EOF'
# MCM Problem C 2021

Team #12345

## Problem: Controlling the Spread of COVID-19

...
EOF

# 运行到 Step 9
```

**预期结果**:
- [x] 章节标题为英文（Introduction / Model Formulation / ...）
- [x] 使用 `article` 或 `mcmthesis`
- [x] 包含 Summary（单段英文摘要）
- [x] 无中文章节

**成功标准**:
- [x] `grep "\\section{Introduction}" *_paper.tex` 找到匹配
- [x] `grep "问题重述" *_paper.tex` 返回空

---

### B4. 代码附录精简工具验证

**目标**: 验证 generate_code_appendix.py 正确精简代码

**实验设计**:

#### B4.1 CUMCM 模式（完整）
```bash
cd complete/test_cumcm2024b/
python3 ../../scripts/generate_code_appendix.py \
    "$(pwd)" test_cumcm2024b --mode=cumcm

# 检查输出
cat paper/appendix_code.tex | grep "subsection{" | wc -l
# 预期: 所有 .py 文件数量（如 6 个）
```

**成功标准**:
- [x] 包含所有代码文件
- [x] 估算页数显示（如 "估算页数: 8.5"）

#### B4.2 MCM 模式（精简到 3 页）
```bash
python3 ../../scripts/generate_code_appendix.py \
    "$(pwd)" test_cumcm2024b --mode=mcm --mcm-page-budget=3.0

# 检查输出
cat paper/appendix_code.tex | grep "Priority 1" | wc -l
# 预期: ≥ 1（至少保留核心 03_solve.py）

cat paper/appendix_code.tex | grep "省略"
# 预期: 显示省略的文件清单
```

**成功标准**:
- [x] 精简后页数 ≤ 3.5
- [x] 优先保留 Priority 1-2 文件
- [x] 显示压缩率（如 "压缩率: 35%"）

---

## 🔄 实验组 C: 回归验证

**目标**: 确认修复未引入副作用

### C1. Baseline 重跑
```bash
# 重跑 cumcm2024b baseline
./launch_agents.sh new --no-start verify_regression_baseline \
    "/absolute/path/to/cumcm_2024_b.pdf"
./launch_agents.sh run verify_regression_baseline

# 等待完成后评估
cd evaluation/
./run_evaluation.sh ../complete/verify_regression_baseline --samples 3
```

**成功标准**:
- [x] 总分在 91.6 ± 1.5 范围内（允许随机波动）
- [x] 符号覆盖率 > 0.7
- [x] PROTECTED 假设数 ≥ 6
- [x] 编译成功，PDF 生成

### C2. 消融实验对比
```bash
# 重跑 no_methodlib 消融
./experiments/ablation_no_method_lib.sh --problem B --reps 1

# 对比修复前后
# 修复前: 85.3 分（方法库缺口）
# 修复后: 预期 > 87 分（几何方法补充）
```

**成功标准**:
- [x] no_methodlib 分数提升 1-2 分（方法库补充效果）
- [x] 其他消融分数保持稳定（± 0.5）

---

## 📊 实验组 D: 对比分析

**目标**: 量化修复的改进幅度

### D1. 数据收集表

| 指标 | 修复前 | 修复后 | 改进 |
|------|--------|--------|------|
| **A 题符号覆盖率** | 0.408 | [待测] | [待计算] |
| **编译失败定位时间** | > 10 分钟 | [待测] | [待计算] |
| **灵敏度报告行数（B 题）** | 176 行 | [待测] | [待计算] |
| **代码附录页数（MCM）** | 8-10 页 | [待测] | [待计算] |
| **PROTECTED 假设数（B 题）** | 8 个 | [待测] | [待计算] |

### D2. 数据采集脚本
```bash
#!/bin/bash
# collect_verification_data.sh

PROJECT=$1

echo "=== 验证数据采集 ==="
echo "项目: $PROJECT"
echo ""

# 符号覆盖率
if [[ -f "$PROJECT/code_review.md" ]]; then
    echo "符号覆盖率:"
    grep -A 5 "符号覆盖率审计" "$PROJECT/code_review.md" | grep "覆盖率"
fi

# 假设数量
if [[ -f "$PROJECT/assumption_ledger.md" ]]; then
    echo "PROTECTED 假设数:"
    grep "PROTECTED" "$PROJECT/assumption_ledger.md" | wc -l
fi

# 灵敏度报告
if [[ -f "$PROJECT/sensitivity_report.md" ]]; then
    echo "灵敏度报告行数:"
    wc -l "$PROJECT/sensitivity_report.md"
    echo "灵敏度图数量:"
    ls "$PROJECT/figures/sensitivity_"*.pdf 2>/dev/null | wc -l
fi

# 代码附录
if [[ -f "$PROJECT/paper/appendix_code.tex" ]]; then
    echo "代码附录页数估算:"
    grep "估算页数" "$PROJECT/paper/appendix_code.tex" || echo "未找到"
fi
```

---

## 📅 实验时间表

### 第 1 天（P0 验证）
- **上午**：A1.1 + A1.2（符号覆盖率验证）
- **下午**：A2（ABSTRACT 验证）+ A3（假设数量验证）
- **晚上**：A4（方法库验证）

### 第 2 天（P1 验证）
- **上午**：B1（灵敏度验证）+ B2（编译诊断验证）
- **下午**：B3（章节动态化验证）+ B4（代码精简验证）
- **晚上**：整理 B 组数据

### 第 3 天（回归 + 长时间实验）
- **全天**：C1（baseline 重跑，2-3 小时）+ C2（消融实验，4-6 小时）

### 第 4 天（数据分析）
- **上午**：D1 + D2（数据采集和对比分析）
- **下午**：撰写验证报告，生成图表
- **晚上**：总结 + 建议

### 第 5 天（补充实验，可选）
- **按需**：边界情况测试、压力测试、用户反馈收集

---

## 🎯 成功标准总结

### P0 验证（必须全部通过）
- [ ] A1: 符号覆盖率门禁正确触发（PASS/FAIL）
- [ ] A2: ABSTRACT 编译成功（无裸下划线错误）
- [ ] A3: 假设数量门禁正确触发（≥ 4 个）
- [ ] A4: 3 个新方法可检索

### P1 验证（必须全部通过）
- [ ] B1: 灵敏度自检正确判定（≥ 150 行 + 2 图）
- [ ] B2: 编译诊断正确提取错误（秒级定位）
- [ ] B3: 章节动态化正确识别（CUMCM/MCM）
- [ ] B4: 代码精简正确压缩（MCM ≤ 3.5 页）

### 回归验证（允许小幅波动）
- [ ] C1: Baseline 分数 91.6 ± 1.5
- [ ] C2: 消融分数提升 1-2 分（no_methodlib）

### 数据对比（达成预期改进）
- [ ] D1: 至少 6/8 指标达到预期改进幅度
- [ ] D2: 无负向影响（如编译时间增加）

---

## 📋 实验交付物

1. **验证数据表**（Excel / CSV）
   - 每个实验的原始数据
   - 修复前后对比
   - 统计显著性检验

2. **验证报告**（Markdown）
   - 每个实验的执行记录
   - 成功/失败标记
   - 异常情况说明

3. **修复效果图表**（PNG / PDF）
   - 符号覆盖率分布图
   - 编译时间对比图
   - 灵敏度完整性对比

4. **问题清单**（如有）
   - 验证中发现的新问题
   - 优先级和修复建议

---

## 🚨 风险与应对

| 风险 | 概率 | 影响 | 应对措施 |
|------|------|------|---------|
| **A 题运行超时** | 中 | 高 | 设置 72 小时 timeout，失败后分析中间状态 |
| **编译诊断误判** | 低 | 中 | 收集多个错误类型，优化正则匹配 |
| **MCM 题目缺失** | 中 | 低 | 手工构造最小 MCM 测试用例 |
| **Baseline 分数波动大** | 低 | 中 | 增加重复次数到 K=5，取中位数 |

---

## 📞 后续行动

**立即执行**（本周）:
1. 运行 A1.1 + A1.2（符号覆盖率验证）→ 最高优先级
2. 运行 B2.1（编译诊断验证）→ 快速验证

**短期执行**（下周）:
3. 完成所有 A 组实验（P0 核心验证）
4. 完成所有 B 组实验（P1 功能验证）

**中期执行**（本月）:
5. C1 + C2 回归验证
6. D1 + D2 数据对比分析
7. 撰写完整验证报告

---

**文档版本**: v1.0  
**创建日期**: 2026-06-14  
**预计完成**: 2026-06-19  
**负责人**: Paper Factory Team
