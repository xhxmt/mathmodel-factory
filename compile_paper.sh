#!/usr/bin/env bash
set -euo pipefail

PROJECT="${1:?Usage: $0 <project_dir> <base_name>}"
BASE="${2:?Usage: $0 <project_dir> <base_name>}"

cd "$PROJECT"

# 创建编译日志目录
mkdir -p logs/compilation

# 检测编译引擎
ENGINE="pdflatex"
if grep -qE '\\documentclass\s*(\[[^]]*\])?\s*\{(ctex|cumcmthesis|mcmthesis)' "${BASE}_paper.tex" 2>/dev/null \
   || grep -q '\\usepackage{xeCJK}' "${BASE}_paper.tex" 2>/dev/null; then
    ENGINE="xelatex"
fi
echo "$(date '+%Y-%m-%d %H:%M:%S') - 使用编译引擎: $ENGINE" >> logs/compilation/compile.log

# 第一次编译（生成 .aux）
if ! "$ENGINE" -interaction=nonstopmode "${BASE}_paper.tex" > logs/compilation/pass1.log 2>&1; then
    echo "❌ 编译失败：第一次 $ENGINE 编译出错" >&2
    echo "" >&2
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" >&2
    echo "📋 错误诊断（提取自 ${BASE}_paper.log）：" >&2
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" >&2

    # 提取关键错误信息
    if [[ -f "${BASE}_paper.log" ]]; then
        # 提取第一个错误（! 开头）及其上下文
        awk '/^!/{found=1} found{print; if(/^l\.[0-9]+/){print ""; exit}}' "${BASE}_paper.log" | head -20 >&2
        echo "" >&2

        # 常见错误模式诊断
        if grep -q "Undefined control sequence" "${BASE}_paper.log"; then
            echo "🔍 诊断：未定义的控制序列（可能缺少宏包或拼写错误）" >&2
            grep -A2 "Undefined control sequence" "${BASE}_paper.log" | head -5 >&2
        fi

        if grep -q "Missing \\$ inserted" "${BASE}_paper.log"; then
            echo "🔍 诊断：数学模式错误（可能缺少 $ 或 _ 未转义）" >&2
            grep -B1 -A2 "Missing \\$ inserted" "${BASE}_paper.log" | head -8 >&2
        fi

        if grep -q "File.*not found" "${BASE}_paper.log"; then
            echo "🔍 诊断：缺失文件" >&2
            grep "File.*not found" "${BASE}_paper.log" | head -5 >&2
        fi

        if grep -q "! Package" "${BASE}_paper.log"; then
            echo "🔍 诊断：宏包错误" >&2
            grep "! Package" "${BASE}_paper.log" | head -5 >&2
        fi

        echo "" >&2
        echo "💡 完整日志位置: $(pwd)/logs/compilation/pass1.log" >&2
        echo "💡 LaTeX 日志: $(pwd)/${BASE}_paper.log" >&2
    else
        echo "⚠️  无法找到 ${BASE}_paper.log 文件" >&2
        echo "💡 完整输出: $(pwd)/logs/compilation/pass1.log" >&2
    fi
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" >&2
    exit 1
fi

# BibTeX 处理
if [[ -f "${BASE}_paper.aux" ]] && grep -q '\\bibdata' "${BASE}_paper.aux" 2>/dev/null; then
    if ! bibtex "${BASE}_paper" > logs/compilation/bibtex.log 2>&1; then
        echo "⚠️  BibTeX 警告（继续编译）" >&2
        grep -E "^(Warning|Error)" logs/compilation/bibtex.log | head -5 >&2
    fi
fi

# 第二次编译（处理引用）
if ! "$ENGINE" -interaction=nonstopmode "${BASE}_paper.tex" > logs/compilation/pass2.log 2>&1; then
    echo "❌ 编译失败：第二次 $ENGINE 编译出错" >&2
    echo "💡 日志: $(pwd)/logs/compilation/pass2.log" >&2
    exit 1
fi

# 第三次编译（最终化）
if ! "$ENGINE" -interaction=nonstopmode "${BASE}_paper.tex" > logs/compilation/pass3.log 2>&1; then
    echo "❌ 编译失败：第三次 $ENGINE 编译出错" >&2
    echo "💡 日志: $(pwd)/logs/compilation/pass3.log" >&2
    exit 1
fi

# 验证 PDF 生成
if [[ ! -f "${BASE}_paper.pdf" ]]; then
    echo "❌ 编译失败：PDF 未生成" >&2
    echo "💡 检查日志: $(pwd)/logs/compilation/" >&2
    exit 1
fi

# 成功信息
PDF_SIZE=$(du -h "${BASE}_paper.pdf" | cut -f1)
PDF_PAGES=$(pdfinfo "${BASE}_paper.pdf" 2>/dev/null | grep "Pages:" | awk '{print $2}' || echo "unknown")
echo "✅ 编译成功: ${BASE}_paper.pdf (${PDF_SIZE}, ${PDF_PAGES} 页)" >> logs/compilation/compile.log
echo "✅ PDF 已生成: ${BASE}_paper.pdf (${PDF_SIZE}, ${PDF_PAGES} 页)"
