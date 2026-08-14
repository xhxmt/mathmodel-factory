# Kendall's Coefficient of Concordance Test

> 来源：LLM-MM-Agent HMML；本文件由 `scripts/import_hmml.py` 确定性生成。

## 分层位置

Evaluation Methods (Evaluation Methods) → Statistical Evaluation (Statistical Evaluation) → Correlation Test (Correlation Test)

## 建模方法

Kendall's Coefficient of Concordance (W) is a statistical method used to measure the consistency of ratings given by multiple raters to the same set of objects.

## 核心思想

The core idea is to evaluate the relative consistency of ratings by calculating the sum of ranks for each object, the mean rank sum, the sum of squared deviations, and the coefficient of concordance using the formula: \[ W = \frac{S}{\frac{1}{12} K^2 (N^3 - N)} \] where \( K \) is the number of raters, \( N \) is the number of objects, and \( S \) is the sum of squared deviations.

## 典型应用

Kendall's Coefficient of Concordance is used for ordinal data, multiple raters, and assessing the degree of consistency among raters.

## 使用边界

该条目来自广覆盖方法目录，仅用于候选召回；实际采用前必须结合题目数据、假设、求解器与失败模式做二次审查。
