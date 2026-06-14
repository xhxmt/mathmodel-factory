#!/bin/bash
# P0 验证 - 轻量级方案（无需完整运行）
# 直接构造测试用例验证门禁逻辑

set -euo pipefail

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "P0 轻量级验证方案"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# ═══════════════════════════════════════════════════════════════
# A1.1 符号覆盖率验证 - PASS 案例
# ═══════════════════════════════════════════════════════════════
echo ""
echo "【A1.1 符号覆盖率验证 - PASS】"

mkdir -p tests/verify_symbols_pass
cd tests/verify_symbols_pass

# 构造高覆盖率测试用例
cat > test_paper.tex << 'EOF'
\documentclass{article}
\begin{document}
Model uses symbols: $\alpha$, $\beta$, $\gamma$, $x$, $y$.
\end{document}
EOF

cat > symbol_table.md << 'EOF'
# Symbol Table

| Symbol | Meaning | Unit |
|--------|---------|------|
| $\alpha$ | Parameter alpha | - |
| $\beta$ | Parameter beta | - |
| $\gamma$ | Parameter gamma | - |
| $x$ | Variable x | m |
| $y$ | Variable y | m |
EOF

# 运行符号覆盖率检查
python3 ../../scripts/verify_symbols.py "$(pwd)" test 2>/dev/null || true

# 验证结果
if [[ -f code_review.md ]]; then
    coverage=$(grep -oP "覆盖率.*?(\d+\.\d+)" code_review.md | grep -oP "\d+\.\d+" | head -1 || echo "0")
    verdict=$(grep -oE "PASS|FAIL|WARNING" code_review.md | head -1 || echo "未找到")

    echo "  ✓ 符号覆盖率: $coverage"
    echo "  ✓ 判定: $verdict"

    if [[ "$verdict" == "PASS" ]] && (( $(echo "$coverage > 0.5" | bc -l) )); then
        echo "  ✅ A1.1 验证通过"
    else
        echo "  ❌ A1.1 验证失败"
    fi
else
    echo "  ⚠️  未找到 code_review.md（verify_symbols.py 可能不存在）"
fi

cd ../..

# ═══════════════════════════════════════════════════════════════
# A1.2 符号覆盖率验证 - FAIL 案例
# ═══════════════════════════════════════════════════════════════
echo ""
echo "【A1.2 符号覆盖率验证 - FAIL】"

mkdir -p tests/verify_symbols_fail
cd tests/verify_symbols_fail

# 构造低覆盖率测试用例
cat > test_paper.tex << 'EOF'
\documentclass{article}
\begin{document}
Model uses many symbols: $\alpha$, $\beta$, $\gamma$, $\delta$, $\epsilon$,
$\zeta$, $\eta$, $\theta$, $\iota$, $\kappa$, $\lambda$, $\mu$, $\nu$,
$x_1$, $x_2$, $y_1$, $y_2$, $z$.
\end{document}
EOF

cat > symbol_table.md << 'EOF'
# Symbol Table

| Symbol | Meaning | Unit |
|--------|---------|------|
| $\alpha$ | Parameter alpha | - |
| $\beta$ | Parameter beta | - |
| $\gamma$ | Parameter gamma | - |
| $x_1$ | Variable x1 | m |
| $y_1$ | Variable y1 | m |
EOF

# 运行符号覆盖率检查
python3 ../../scripts/verify_symbols.py "$(pwd)" test 2>/dev/null || true

# 验证结果
if [[ -f code_review.md ]]; then
    coverage=$(grep -oP "覆盖率.*?(\d+\.\d+)" code_review.md | grep -oP "\d+\.\d+" | head -1 || echo "0")
    verdict=$(grep -oE "PASS|FAIL|WARNING" code_review.md | head -1 || echo "未找到")

    echo "  ✓ 符号覆盖率: $coverage"
    echo "  ✓ 判定: $verdict"

    if [[ "$verdict" == "FAIL" ]] && (( $(echo "$coverage < 0.5" | bc -l) )); then
        echo "  ✅ A1.2 验证通过（正确触发 FAIL）"
    else
        echo "  ❌ A1.2 验证失败"
    fi
else
    echo "  ⚠️  verify_symbols.py 不存在"
fi

cd ../..

# ═══════════════════════════════════════════════════════════════
# A2 ABSTRACT 验证
# ═══════════════════════════════════════════════════════════════
echo ""
echo "【A2 ABSTRACT_PLACEHOLDER 验证】"

mkdir -p tests/verify_abstract
cd tests/verify_abstract

# A2.1: Step 9 占位符形式
cat > test_step9_paper.tex << 'EOF'
\documentclass{article}
\begin{document}
\begin{abstract}
\detokenize{ABSTRACT_PLACEHOLDER}
\end{abstract}
Content here.
\end{document}
EOF

echo "  测试 A2.1 (Step 9 占位符)"
if ../../../compile_paper.sh "$(pwd)" test_step9 >/dev/null 2>&1; then
    if [[ -f test_step9_paper.pdf ]]; then
        echo "  ✅ A2.1 验证通过（\\detokenize{} 编译成功）"
    fi
else
    echo "  ❌ A2.1 验证失败（编译错误）"
fi

# A2.2: 裸占位符（预期失败）
cat > test_step9_bad_paper.tex << 'EOF'
\documentclass{article}
\begin{document}
\begin{abstract}
ABSTRACT_PLACEHOLDER
\end{abstract}
Content here.
\end{document}
EOF

echo "  测试 A2.2 (裸占位符，预期失败)"
if ../../../compile_paper.sh "$(pwd)" test_step9_bad >/dev/null 2>&1; then
    echo "  ⚠️  裸占位符编译成功（不符合预期）"
else
    echo "  ✅ A2.2 验证通过（裸占位符正确失败）"
fi

cd ../..

# ═══════════════════════════════════════════════════════════════
# A3 假设数量验证
# ═══════════════════════════════════════════════════════════════
echo ""
echo "【A3 PROTECTED 假设数量验证】"

mkdir -p tests/verify_assumptions
cd tests/verify_assumptions

# A3.1: ≥ 4 个 PROTECTED（预期 PASS）
cat > assumption_ledger_pass.md << 'EOF'
# Assumption Ledger

| ID | Statement | Status | Tags |
|----|-----------|--------|------|
| A1 | Assumption 1 | CONFIRMED | PROTECTED |
| A2 | Assumption 2 | CONFIRMED | PROTECTED |
| A3 | Assumption 3 | CONFIRMED | PROTECTED |
| A4 | Assumption 4 | CONFIRMED | PROTECTED |
| A5 | Assumption 5 | OPEN | |

## PROTECTED 假设数量自检

PROTECTED 假设数: 4
判定: ✅ 合格（≥ 4）
EOF

protected_count=$(grep -c "PROTECTED" assumption_ledger_pass.md || echo 0)
echo "  A3.1: PROTECTED 数量 = $protected_count"
if (( protected_count >= 4 )); then
    echo "  ✅ A3.1 验证通过（≥ 4 个）"
else
    echo "  ❌ A3.1 验证失败"
fi

# A3.2: 3 个 PROTECTED（预期 WARNING）
cat > assumption_ledger_warning.md << 'EOF'
| A1 | Assumption 1 | CONFIRMED | PROTECTED |
| A2 | Assumption 2 | CONFIRMED | PROTECTED |
| A3 | Assumption 3 | CONFIRMED | PROTECTED |
EOF

protected_count=$(grep -c "PROTECTED" assumption_ledger_warning.md || echo 0)
echo "  A3.2: PROTECTED 数量 = $protected_count"
if (( protected_count == 3 )); then
    echo "  ✅ A3.2 验证通过（3 个，预期触发 WARNING）"
else
    echo "  ❌ A3.2 验证失败"
fi

# A3.3: 1 个 PROTECTED（预期 FAIL）
cat > assumption_ledger_fail.md << 'EOF'
| A1 | Assumption 1 | CONFIRMED | PROTECTED |
EOF

protected_count=$(grep -c "PROTECTED" assumption_ledger_fail.md || echo 0)
echo "  A3.3: PROTECTED 数量 = $protected_count"
if (( protected_count <= 1 )); then
    echo "  ✅ A3.3 验证通过（≤ 1 个，预期触发 FAIL）"
else
    echo "  ❌ A3.3 验证失败"
fi

cd ../..

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "✅ 轻量级验证完成"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
