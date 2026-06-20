#!/bin/bash
# 验证实验数据采集脚本
# 用途：从完成的项目中提取关键指标，用于对比修复前后效果

set -euo pipefail

PROJECT="${1:?Usage: $0 <project_dir>}"

if [[ ! -d "$PROJECT" ]]; then
    echo "❌ 项目目录不存在: $PROJECT" >&2
    exit 1
fi

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "📊 验证数据采集"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "项目: $PROJECT"
echo "采集时间: $(date '+%Y-%m-%d %H:%M:%S')"
echo ""

# ═══════════════════════════════════════════════════════════════
# P0.1 符号覆盖率
# ═══════════════════════════════════════════════════════════════
echo "【P0.1 符号覆盖率】"

if [[ -f "$PROJECT/code_review.md" ]]; then
    # 提取符号覆盖率数值
    coverage=$(grep -A 5 "符号覆盖率审计" "$PROJECT/code_review.md" 2>/dev/null | \
               grep -oP "覆盖率.*?(\d+\.\d+|\d+%)" | \
               grep -oP "\d+\.\d+" | head -1 || echo "未找到")

    symbols_used=$(grep "论文中使用的符号总数" "$PROJECT/code_review.md" 2>/dev/null | \
                   grep -oP "\d+" | head -1 || echo "未找到")

    symbols_defined=$(grep "已在 symbol_table.md 登记" "$PROJECT/code_review.md" 2>/dev/null | \
                      grep -oP "\d+" | head -1 || echo "未找到")

    undefined_count=$(grep "未登记符号数" "$PROJECT/code_review.md" 2>/dev/null | \
                      grep -oP "\d+" | head -1 || echo "未找到")

    verdict=$(grep -A 1 "判定:" "$PROJECT/code_review.md" 2>/dev/null | \
              grep -oE "PASS|WARNING|FAIL" | head -1 || echo "未找到")

    echo "  ✓ 符号覆盖率: ${coverage}"
    echo "  ✓ 论文使用符号: ${symbols_used}"
    echo "  ✓ 已登记符号: ${symbols_defined}"
    echo "  ✓ 未登记符号: ${undefined_count}"
    echo "  ✓ 判定: ${verdict}"
else
    echo "  ⚠️  未找到 code_review.md"
fi
echo ""

# ═══════════════════════════════════════════════════════════════
# P0.2 ABSTRACT 编译
# ═══════════════════════════════════════════════════════════════
echo "【P0.2 ABSTRACT 编译】"

if [[ -f "$PROJECT"/*_paper.tex ]]; then
    paper_tex=$(ls "$PROJECT"/*_paper.tex | head -1)

    # 检查占位符形式
    if grep -q "\\\\detokenize{ABSTRACT_PLACEHOLDER}" "$paper_tex"; then
        echo "  ✓ Step 9: 使用 \\detokenize{} 转义"
    elif grep -q "ABSTRACT_PLACEHOLDER" "$paper_tex"; then
        echo "  ⚠️  Step 9: 发现裸 ABSTRACT_PLACEHOLDER（可能编译失败）"
    else
        echo "  ✓ Step 14: ABSTRACT_PLACEHOLDER 已被替换"
    fi

    # 检查编译日志
    if [[ -f "$PROJECT/logs/compilation/compile.log" ]]; then
        compile_success=$(grep -c "✅ PDF 已生成" "$PROJECT/logs/compilation/compile.log" 2>/dev/null || echo 0)
        echo "  ✓ 编译成功次数: ${compile_success}"

        if [[ -f "$PROJECT/logs/compilation/pass1.log" ]]; then
            error_count=$(grep -c "^!" "$PROJECT/logs/compilation/pass1.log" 2>/dev/null || echo 0)
            echo "  ✓ 编译错误数: ${error_count}"
        fi
    fi

    # 检查 PDF
    if [[ -f "$PROJECT"/*_paper.pdf ]]; then
        pdf_file=$(ls "$PROJECT"/*_paper.pdf | head -1)
        pdf_size=$(du -h "$pdf_file" | cut -f1)
        pdf_pages=$(pdfinfo "$pdf_file" 2>/dev/null | grep "Pages:" | awk '{print $2}' || echo "unknown")
        echo "  ✓ PDF 文件: ${pdf_size}, ${pdf_pages} 页"
    else
        echo "  ⚠️  未找到 PDF 文件"
    fi
else
    echo "  ⚠️  未找到 .tex 文件"
fi
echo ""

# ═══════════════════════════════════════════════════════════════
# P0.3 PROTECTED 假设数量
# ═══════════════════════════════════════════════════════════════
echo "【P0.3 PROTECTED 假设数量】"

if [[ -f "$PROJECT/assumption_ledger.md" ]]; then
    protected_count=$(grep -c "PROTECTED" "$PROJECT/assumption_ledger.md" 2>/dev/null || echo 0)
    total_assumptions=$(grep -c "^| A" "$PROJECT/assumption_ledger.md" 2>/dev/null || echo 0)

    echo "  ✓ PROTECTED 假设数: ${protected_count}"
    echo "  ✓ 假设总数: ${total_assumptions}"

    # 检查自检章节
    if grep -q "PROTECTED 假设数量自检" "$PROJECT/assumption_ledger.md"; then
        self_check=$(grep -A 2 "判定:" "$PROJECT/assumption_ledger.md" | \
                     grep -oE "合格|需补充|不合格" | head -1 || echo "未找到")
        echo "  ✓ 自检判定: ${self_check}"
    fi

    # 假设状态分布
    open_count=$(grep -c "| OPEN |" "$PROJECT/assumption_ledger.md" 2>/dev/null || echo 0)
    confirmed_count=$(grep -c "| CONFIRMED |" "$PROJECT/assumption_ledger.md" 2>/dev/null || echo 0)
    echo "  ✓ OPEN: ${open_count}, CONFIRMED: ${confirmed_count}"
else
    echo "  ⚠️  未找到 assumption_ledger.md"
fi
echo ""

# ═══════════════════════════════════════════════════════════════
# P0.4 方法库使用
# ═══════════════════════════════════════════════════════════════
echo "【P0.4 方法库使用】"

if [[ -f "$PROJECT/method_decision.md" ]]; then
    primary_method=$(grep "^PRIMARY:" "$PROJECT/method_decision.md" 2>/dev/null | head -1 || echo "未找到")
    echo "  ✓ PRIMARY 方法: ${primary_method}"

    # 检查是否使用新增几何方法
    if grep -qE "broad_phase_collision|curve_joining|feasibility_bisection" "$PROJECT/method_decision.md"; then
        echo "  ✓ 使用新增几何方法"
    fi
fi
echo ""

# ═══════════════════════════════════════════════════════════════
# P1.1 灵敏度分析完整性
# ═══════════════════════════════════════════════════════════════
echo "【P1.1 灵敏度分析完整性】"

if [[ -f "$PROJECT/sensitivity_report.md" ]]; then
    sens_lines=$(wc -l < "$PROJECT/sensitivity_report.md")
    echo "  ✓ sensitivity_report.md 行数: ${sens_lines}"

    # 图数量
    tornado_count=$(ls "$PROJECT/figures/sensitivity_tornado"*.pdf 2>/dev/null | wc -l || echo 0)
    scenario_count=$(ls "$PROJECT/figures/sensitivity_scenario"*.pdf 2>/dev/null | wc -l || echo 0)
    total_sens_figs=$(ls "$PROJECT/figures/sensitivity_"*.pdf 2>/dev/null | wc -l || echo 0)

    echo "  ✓ 灵敏度图总数: ${total_sens_figs}"
    echo "    - Tornado 图: ${tornado_count}"
    echo "    - Scenario 图: ${scenario_count}"

    # 自检判定
    if grep -q "灵敏度分析最小覆盖自检" "$PROJECT/sensitivity_report.md"; then
        sens_verdict=$(grep -A 2 "判定:" "$PROJECT/sensitivity_report.md" | \
                       grep -oE "PASS|不足|FAIL" | head -1 || echo "未找到")
        echo "  ✓ 自检判定: ${sens_verdict}"
    fi

    # 假设状态升级数
    if [[ -f "$PROJECT/assumption_ledger.md" ]]; then
        upgrade_count=$(grep -c "→" "$PROJECT/assumption_ledger.md" 2>/dev/null || echo 0)
        echo "  ✓ 假设状态变更数: ${upgrade_count}"
    fi
else
    echo "  ⚠️  未找到 sensitivity_report.md"
fi
echo ""

# ═══════════════════════════════════════════════════════════════
# P1.2 编译诊断
# ═══════════════════════════════════════════════════════════════
echo "【P1.2 编译诊断】"

if [[ -d "$PROJECT/logs/compilation" ]]; then
    echo "  ✓ 编译日志目录存在"

    # 日志文件数
    log_files=$(ls "$PROJECT/logs/compilation/"*.log 2>/dev/null | wc -l || echo 0)
    echo "  ✓ 日志文件数: ${log_files}"

    # 编译总次数
    if [[ -f "$PROJECT/logs/compilation/compile.log" ]]; then
        compile_attempts=$(grep -c "使用编译引擎" "$PROJECT/logs/compilation/compile.log" 2>/dev/null || echo 0)
        echo "  ✓ 编译总次数: ${compile_attempts}"
    fi
else
    echo "  ⚠️  未找到编译日志目录"
fi
echo ""

# ═══════════════════════════════════════════════════════════════
# P1.3 章节结构
# ═══════════════════════════════════════════════════════════════
echo "【P1.3 章节结构】"

if [[ -f "$PROJECT"/*_paper.tex ]]; then
    paper_tex=$(ls "$PROJECT"/*_paper.tex | head -1)

    # 检测竞赛类型
    if grep -qE "ctexart|xeCJK|cumcmthesis" "$paper_tex"; then
        contest_type="CUMCM"
    elif grep -qE "mcmthesis" "$paper_tex"; then
        contest_type="MCM/ICM"
    else
        contest_type="Unknown"
    fi
    echo "  ✓ 检测竞赛类型: ${contest_type}"

    # 章节语言
    chinese_sections=$(grep -c "\\\\section{问题" "$paper_tex" 2>/dev/null || echo 0)
    english_sections=$(grep -c "\\\\section{Introduction\|Model" "$paper_tex" 2>/dev/null || echo 0)

    if (( chinese_sections > 0 )); then
        echo "  ✓ 章节语言: 中文（${chinese_sections} 个中文章节）"
    elif (( english_sections > 0 )); then
        echo "  ✓ 章节语言: 英文（${english_sections} 个英文章节）"
    fi

    # 章节总数
    section_count=$(grep -c "\\\\section{" "$paper_tex" 2>/dev/null || echo 0)
    echo "  ✓ 章节总数: ${section_count}"
fi
echo ""

# ═══════════════════════════════════════════════════════════════
# P1.4 代码附录
# ═══════════════════════════════════════════════════════════════
echo "【P1.4 代码附录】"

if [[ -f "$PROJECT/paper/appendix_code.tex" ]]; then
    echo "  ✓ 代码附录文件存在"

    # 提取页数估算
    estimated_pages=$(grep "估算页数" "$PROJECT/paper/appendix_code.tex" 2>/dev/null | \
                      grep -oP "\d+\.\d+" | head -1 || echo "未找到")
    echo "  ✓ 估算页数: ${estimated_pages}"

    # 文件数
    file_count=$(grep -c "subsection{" "$PROJECT/paper/appendix_code.tex" 2>/dev/null || echo 0)
    echo "  ✓ 代码文件数: ${file_count}"

    # 检测模式
    if grep -q "Mode: MCM" "$PROJECT/paper/appendix_code.tex"; then
        echo "  ✓ 精简模式: MCM"
    elif grep -q "Mode: CUMCM" "$PROJECT/paper/appendix_code.tex"; then
        echo "  ✓ 精简模式: CUMCM（完整）"
    fi
else
    echo "  ⚠️  未找到 paper/appendix_code.tex"

    # 检查是否有直接嵌入的代码
    if [[ -f "$PROJECT"/*_paper.tex ]]; then
        code_lines=$(grep -c "\\\\lstinputlisting\|\\\\begin{lstlisting}" "$PROJECT"/*_paper.tex 2>/dev/null || echo 0)
        echo "  ✓ 论文中代码块数: ${code_lines}"
    fi
fi
echo ""

# ═══════════════════════════════════════════════════════════════
# 总体完成度
# ═══════════════════════════════════════════════════════════════
echo "【总体完成度】"

if [[ -f "$PROJECT/checkpoint.md" ]]; then
    last_step=$(grep "Last completed step" "$PROJECT/checkpoint.md" 2>/dev/null | \
                grep -oP "\d+" | tail -1 || echo "未找到")
    echo "  ✓ 最后完成步骤: Step ${last_step}"

    if [[ "$last_step" == "16" ]]; then
        echo "  ✅ 项目完成"
    fi
fi

# 项目评分（如存在）
if [[ -f "$PROJECT/../evaluation_results.json" ]]; then
    base_name=$(basename "$PROJECT")
    total_score=$(python3 -c "
import json, sys
try:
    with open('$PROJECT/../evaluation_results.json') as f:
        data = json.load(f)
        score = data.get('$base_name', {}).get('total_score', 'N/A')
        print(score)
except:
    print('N/A')
" 2>/dev/null || echo "N/A")

    if [[ "$total_score" != "N/A" ]]; then
        echo "  ✓ 评分: ${total_score}"
    fi
fi

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "✅ 数据采集完成"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
