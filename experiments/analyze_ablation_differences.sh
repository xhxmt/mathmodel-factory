#!/bin/bash
# 消融实验深度分析脚本
# 对比基线和各消融项目的关键差异

set -e

REPORT_FILE="evaluation/ablation_deep_dive_analysis.md"

cat > "$REPORT_FILE" << 'HEADER'
# 消融实验深度分析报告

> **生成日期**: $(date +%Y-%m-%d)
> **目的**: 理解各消融条件下流水线的实际行为差异

---

## 分析方法

本报告通过对比6个项目的关键决策文件，提取以下信息：
1. 方法选择差异（`method_decision.md`）
2. 模型复杂度差异（`model.md` 行数、符号数）
3. 假设保护情况（`assumption_ledger.md` 中PROTECTED数量）
4. 数值一致性（`verify_numbers` 的UNMATCHED计数）
5. 文献引用质量（`references.bib` 引用数）

HEADER

echo "## 1. 方法选择对比" >> "$REPORT_FILE"
echo "" >> "$REPORT_FILE"
echo "| 项目 | PRIMARY方法 | AUXILIARY方法 | 创新性评分 |" >> "$REPORT_FILE"
echo "|---|---|---|---:|" >> "$REPORT_FILE"

for project in test_cumcm2024b cumcm2024b_no_consult_rep1 cumcm2024b_no_innov_rep1 cumcm2024b_no_judge_rep1 cumcm2024b_no_methodlib_rep1; do
    if [ -f "complete/$project/method_decision.md" ]; then
        primary=$(grep "^DECISION: PRIMARY=" "complete/$project/method_decision.md" | head -1 | sed 's/DECISION: PRIMARY=//' | cut -d' ' -f1)
        auxiliary=$(grep "^DECISION:.*AUXILIARY=" "complete/$project/method_decision.md" | head -1 | sed 's/.*AUXILIARY=//' | cut -d' ' -f1)
        innovation=$(grep -A1 "| m3 |" "complete/$project/method_decision.md" | tail -1 | awk '{print $2}' | head -1)

        echo "| $project | $primary | $auxiliary | ${innovation:-N/A} |" >> "$REPORT_FILE"
    fi
done

echo "" >> "$REPORT_FILE"
echo "## 2. 模型复杂度对比" >> "$REPORT_FILE"
echo "" >> "$REPORT_FILE"
echo "| 项目 | model.md行数 | symbol_table.md行数 | assumption_ledger.md行数 |" >> "$REPORT_FILE"
echo "|---|---:|---:|---:|" >> "$REPORT_FILE"

for project in test_cumcm2024b cumcm2024b_no_consult_rep1 cumcm2024b_no_innov_rep1 cumcm2024b_no_judge_rep1 cumcm2024b_no_methodlib_rep1; do
    model_lines=$(wc -l < "complete/$project/model.md" 2>/dev/null || echo "0")
    symbol_lines=$(wc -l < "complete/$project/symbol_table.md" 2>/dev/null || echo "0")
    assumption_lines=$(wc -l < "complete/$project/assumption_ledger.md" 2>/dev/null || echo "0")

    echo "| $project | $model_lines | $symbol_lines | $assumption_lines |" >> "$REPORT_FILE"
done

echo "" >> "$REPORT_FILE"
echo "## 3. PROTECTED假设统计" >> "$REPORT_FILE"
echo "" >> "$REPORT_FILE"
echo "| 项目 | PROTECTED数量 | CHALLENGED数量 | RESOLVED数量 |" >> "$REPORT_FILE"
echo "|---|---:|---:|---:|" >> "$REPORT_FILE"

for project in test_cumcm2024b cumcm2024b_no_consult_rep1 cumcm2024b_no_innov_rep1 cumcm2024b_no_judge_rep1 cumcm2024b_no_methodlib_rep1; do
    if [ -f "complete/$project/assumption_ledger.md" ]; then
        protected=$(grep -c "PROTECTED" "complete/$project/assumption_ledger.md" 2>/dev/null || echo "0")
        challenged=$(grep -c "CHALLENGED" "complete/$project/assumption_ledger.md" 2>/dev/null || echo "0")
        resolved=$(grep -c "RESOLVED" "complete/$project/assumption_ledger.md" 2>/dev/null || echo "0")

        echo "| $project | $protected | $challenged | $resolved |" >> "$REPORT_FILE"
    fi
done

echo "" >> "$REPORT_FILE"
echo "## 4. 引用文献数量" >> "$REPORT_FILE"
echo "" >> "$REPORT_FILE"
echo "| 项目 | .bib条目数 | 论文中引用数 |" >> "$REPORT_FILE"
echo "|---|---:|---:|" >> "$REPORT_FILE"

for project in test_cumcm2024b cumcm2024b_no_consult_rep1 cumcm2024b_no_innov_rep1 cumcm2024b_no_judge_rep1 cumcm2024b_no_methodlib_rep1; do
    bib_count=$(grep -c "^@" "complete/$project/references.bib" 2>/dev/null || echo "0")
    cite_count=$(grep -o "\\\\cite{" "complete/$project/paper.tex" 2>/dev/null | wc -l || echo "0")

    echo "| $project | $bib_count | $cite_count |" >> "$REPORT_FILE"
done

echo "" >> "$REPORT_FILE"
echo "## 5. 数值一致性检查" >> "$REPORT_FILE"
echo "" >> "$REPORT_FILE"
echo "运行 verify_numbers.py 检查各项目的数值一致性..." >> "$REPORT_FILE"
echo "" >> "$REPORT_FILE"
echo "| 项目 | UNMATCHED数量 | 一致性状态 |" >> "$REPORT_FILE"
echo "|---|---:|---|" >> "$REPORT_FILE"

for project in test_cumcm2024b cumcm2024b_no_consult_rep1 cumcm2024b_no_innov_rep1 cumcm2024b_no_judge_rep1 cumcm2024b_no_methodlib_rep1; do
    # 提取评估JSON中的unmatched计数
    if [ -f "evaluation/results/${project}_eval.json" ]; then
        unmatched=$(python3 -c "import json; d=json.load(open('evaluation/results/${project}_eval.json')); print(d.get('verify_numbers_unmatched', 'N/A'))" 2>/dev/null || echo "N/A")
        status="✓"
        [ "$unmatched" != "0" ] && [ "$unmatched" != "N/A" ] && status="⚠️"

        echo "| $project | $unmatched | $status |" >> "$REPORT_FILE"
    fi
done

echo "" >> "$REPORT_FILE"
echo "---" >> "$REPORT_FILE"
echo "" >> "$REPORT_FILE"
echo "**生成时间**: $(date)" >> "$REPORT_FILE"

echo "✓ 深度分析报告已生成: $REPORT_FILE"
