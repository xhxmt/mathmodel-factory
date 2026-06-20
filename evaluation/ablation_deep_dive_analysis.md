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

## 1. 方法选择对比

| 项目 | PRIMARY方法 | AUXILIARY方法 | 创新性评分 |
|---|---|---|---:|
| test_cumcm2024b | m3 | m2 | N/A |
| cumcm2024b_no_consult_rep1 | m2 | m3 | N/A |
| cumcm2024b_no_innov_rep1 | m3 | m4 | m4 |
| cumcm2024b_no_judge_rep1 | m2 | m1 | N/A |
| cumcm2024b_no_methodlib_rep1 | m3 | m2 | N/A |

## 2. 模型复杂度对比

| 项目 | model.md行数 | symbol_table.md行数 | assumption_ledger.md行数 |
|---|---:|---:|---:|
| test_cumcm2024b | 124 | 64 | 59 |
| cumcm2024b_no_consult_rep1 | 128 | 65 | 34 |
| cumcm2024b_no_innov_rep1 | 143 | 68 | 57 |
| cumcm2024b_no_judge_rep1 | 170 | 65 | 45 |
| cumcm2024b_no_methodlib_rep1 | 120 | 63 | 31 |

## 3. PROTECTED假设统计

| 项目 | PROTECTED数量 | CHALLENGED数量 | RESOLVED数量 |
|---|---:|---:|---:|
| test_cumcm2024b | 8 | 0
0 | 1 |
| cumcm2024b_no_consult_rep1 | 5 | 0
0 | 1 |
| cumcm2024b_no_innov_rep1 | 7 | 0
0 | 11 |
| cumcm2024b_no_judge_rep1 | 5 | 0
0 | 1 |
| cumcm2024b_no_methodlib_rep1 | 2 | 0
0 | 1 |

## 4. 引用文献数量

| 项目 | .bib条目数 | 论文中引用数 |
|---|---:|---:|
| test_cumcm2024b | 12 | 0 |
| cumcm2024b_no_consult_rep1 | 10 | 0 |
| cumcm2024b_no_innov_rep1 | 10 | 0 |
| cumcm2024b_no_judge_rep1 | 10 | 0 |
| cumcm2024b_no_methodlib_rep1 | 10 | 0 |

## 5. 数值一致性检查

运行 verify_numbers.py 检查各项目的数值一致性...

| 项目 | UNMATCHED数量 | 一致性状态 |
|---|---:|---|
| test_cumcm2024b | N/A | ✓ |
| cumcm2024b_no_consult_rep1 | N/A | ✓ |
| cumcm2024b_no_innov_rep1 | N/A | ✓ |
| cumcm2024b_no_judge_rep1 | N/A | ✓ |
| cumcm2024b_no_methodlib_rep1 | N/A | ✓ |

---

**生成时间**: 2026年 06月 13日 星期六 01:00:19 UTC
