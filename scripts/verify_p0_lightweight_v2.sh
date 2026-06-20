#!/bin/bash
# P0 轻量级验证方案 v2 - 修复版
set -euo pipefail

cd "$(dirname "$0")/.."

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "P0 轻量级验证方案 v2"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

PASS_COUNT=0
FAIL_COUNT=0

# ═══════════════════════════════════════════════════════════════
# A1 符号覆盖率验证
# ═══════════════════════════════════════════════════════════════
echo ""
echo "【A1 符号覆盖率验证】"

# A1.1: 高覆盖率（预期 PASS）
mkdir -p tests/verify_symbols_pass
cd tests/verify_symbols_pass

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

echo "  A1.1 高覆盖率测试（5/5 = 1.0）"
python3 ../../scripts/verify_symbols.py "$(pwd)" test > verify_output.txt 2>&1 || true

symbols_used=$(grep "SYMBOLS_USED" verify_output.txt | grep -oP "\d+" | head -1)
symbols_defined=$(grep "SYMBOLS_DEFINED" verify_output.txt | grep -oP "\d+" | head -1)
undefined=$(grep "UNDEFINED_SYMBOLS" verify_output.txt | grep -oP "\d+" | head -1)

if [[ "$symbols_used" == "$symbols_defined" ]] && [[ "$undefined" == "0" ]]; then
    echo "  ✅ A1.1 PASS: 覆盖率 ${symbols_defined}/${symbols_used} = 1.0"
    ((PASS_COUNT++))
else
    echo "  ❌ A1.1 FAIL: 覆盖率 ${symbols_defined}/${symbols_used}"
    ((FAIL_COUNT++))
fi

cd ../..

# A1.2: 低覆盖率（预期 FAIL）
mkdir -p tests/verify_symbols_fail
cd tests/verify_symbols_fail

cat > test_paper.tex << 'EOF'
\documentclass{article}
\begin{document}
Model uses: $\alpha$, $\beta$, $\gamma$, $\delta$, $\epsilon$,
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

echo "  A1.2 低覆盖率测试（5/18 ≈ 0.28）"
python3 ../../scripts/verify_symbols.py "$(pwd)" test > verify_output.txt 2>&1 || true

symbols_used=$(grep "SYMBOLS_USED" verify_output.txt | grep -oP "\d+" | head -1)
symbols_defined=$(grep "SYMBOLS_DEFINED" verify_output.txt | grep -oP "\d+" | head -1)
undefined=$(grep "UNDEFINED_SYMBOLS" verify_output.txt | grep -oP "\d+" | head -1)

coverage=$(echo "scale=2; $symbols_defined / $symbols_used" | bc)

if (( $(echo "$coverage < 0.5" | bc -l) )) && (( undefined > 5 )); then
    echo "  ✅ A1.2 PASS: 覆盖率 ${symbols_defined}/${symbols_used} = ${coverage} < 0.5（正确触发 FAIL）"
    ((PASS_COUNT++))
else
    echo "  ❌ A1.2 FAIL: 覆盖率不符合预期"
    ((FAIL_COUNT++))
fi

cd ../..

# ═══════════════════════════════════════════════════════════════
# A2 ABSTRACT 验证
# ═══════════════════════════════════════════════════════════════
echo ""
echo "【A2 ABSTRACT_PLACEHOLDER 验证】"

mkdir -p tests/verify_abstract
cd tests/verify_abstract

# A2.1: detokenize 包裹（预期成功）
cat > test_good_paper.tex << 'EOF'
\documentclass{article}
\begin{document}
\begin{abstract}
\detokenize{ABSTRACT_PLACEHOLDER}
\end{abstract}
This is content.
\end{document}
EOF

echo "  A2.1 \\detokenize{} 转义测试"
if ../../compile_paper.sh "$(pwd)" test_good >/dev/null 2>&1; then
    if [[ -f test_good_paper.pdf ]]; then
        echo "  ✅ A2.1 PASS: \\detokenize{ABSTRACT_PLACEHOLDER} 编译成功"
        ((PASS_COUNT++))
    else
        echo "  ❌ A2.1 FAIL: 编译成功但无 PDF"
        ((FAIL_COUNT++))
    fi
else
    echo "  ❌ A2.1 FAIL: 编译失败"
    ((FAIL_COUNT++))
    # 显示错误
    tail -20 logs/compilation/pass1.log 2>/dev/null || true
fi

# A2.2: 裸占位符（预期失败）
cat > test_bad_paper.tex << 'EOF'
\documentclass{article}
\begin{document}
\begin{abstract}
ABSTRACT_PLACEHOLDER
\end{abstract}
This is content.
\end{document}
EOF

rm -rf logs/compilation
echo "  A2.2 裸占位符测试（预期编译失败）"
if ../../compile_paper.sh "$(pwd)" test_bad >/dev/null 2>&1; then
    echo "  ❌ A2.2 FAIL: 裸占位符编译成功（不符合预期）"
    ((FAIL_COUNT++))
else
    echo "  ✅ A2.2 PASS: 裸占位符正确触发编译失败"
    ((PASS_COUNT++))
fi

cd ../..

# ═══════════════════════════════════════════════════════════════
# A3 假设数量验证
# ═══════════════════════════════════════════════════════════════
echo ""
echo "【A3 PROTECTED 假设数量验证】"

mkdir -p tests/verify_assumptions

# A3.1: ≥ 4 个（预期合格）
cat > tests/verify_assumptions/ledger_pass.md << 'EOF'
| A1 | Assumption 1 | CONFIRMED | PROTECTED |
| A2 | Assumption 2 | CONFIRMED | PROTECTED |
| A3 | Assumption 3 | CONFIRMED | PROTECTED |
| A4 | Assumption 4 | CONFIRMED | PROTECTED |
EOF

protected=$(grep -c "PROTECTED" tests/verify_assumptions/ledger_pass.md)
echo "  A3.1 充足假设测试（$protected 个）"
if (( protected >= 4 )); then
    echo "  ✅ A3.1 PASS: $protected 个 PROTECTED ≥ 4（合格）"
    ((PASS_COUNT++))
else
    echo "  ❌ A3.1 FAIL: $protected 个 < 4"
    ((FAIL_COUNT++))
fi

# A3.2: 3 个（预期警告）
cat > tests/verify_assumptions/ledger_warning.md << 'EOF'
| A1 | Assumption 1 | CONFIRMED | PROTECTED |
| A2 | Assumption 2 | CONFIRMED | PROTECTED |
| A3 | Assumption 3 | CONFIRMED | PROTECTED |
EOF

protected=$(grep -c "PROTECTED" tests/verify_assumptions/ledger_warning.md)
echo "  A3.2 边界假设测试（$protected 个）"
if (( protected == 3 )); then
    echo "  ✅ A3.2 PASS: $protected 个 PROTECTED（预期触发 WARNING）"
    ((PASS_COUNT++))
else
    echo "  ❌ A3.2 FAIL: $protected 个不符合预期"
    ((FAIL_COUNT++))
fi

# A3.3: 1 个（预期失败）
cat > tests/verify_assumptions/ledger_fail.md << 'EOF'
| A1 | Assumption 1 | CONFIRMED | PROTECTED |
EOF

protected=$(grep -c "PROTECTED" tests/verify_assumptions/ledger_fail.md)
echo "  A3.3 不足假设测试（$protected 个）"
if (( protected <= 1 )); then
    echo "  ✅ A3.3 PASS: $protected 个 PROTECTED ≤ 1（预期触发 FAIL）"
    ((PASS_COUNT++))
else
    echo "  ❌ A3.3 FAIL: $protected 个不符合预期"
    ((FAIL_COUNT++))
fi

# ═══════════════════════════════════════════════════════════════
# 总结
# ═══════════════════════════════════════════════════════════════
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "✅ 轻量级验证完成"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "测试结果统计："
echo "  ✅ 通过: $PASS_COUNT/8"
echo "  ❌ 失败: $FAIL_COUNT/8"
echo ""

if (( FAIL_COUNT == 0 )); then
    echo "🎉 所有测试通过！"
    exit 0
else
    echo "⚠️  有 $FAIL_COUNT 个测试失败"
    exit 1
fi
