# Wilcoxon's Signed Rank Test

> 来源：LLM-MM-Agent HMML；本文件由 `scripts/import_hmml.py` 确定性生成。

## 分层位置

Evaluation Methods (Evaluation Methods) → Statistical Evaluation (Statistical Evaluation) → Correlation Test (Correlation Test)

## 建模方法

The Wilcoxon Signed Rank Test is a non-parametric statistical method used to compare the differences between two related samples or paired observations, especially when the data does not meet the normal distribution assumption.

## 核心思想

The test involves the following steps: 1. Calculate the differences: Compute the difference for each pair of observations. 2. Remove zero differences: Exclude pairs with zero differences. 3. Rank the differences: Rank the absolute values of the remaining differences. 4. Assign signs: Assign the original signs (positive or negative) to the ranks. 5. Calculate the test statistic: Sum the positive and negative ranks separately, and take the smaller of the two sums as the test statistic. 6. Significance test: Compare the test statistic with the critical value or calculate the p-value to determine if the difference is significant.

## 典型应用

The Wilcoxon Signed Rank Test is suitable for paired samples, continuous or ordinal data, and does not require the data to follow a normal distribution but assumes the distribution of differences is symmetric.

## 使用边界

该条目来自广覆盖方法目录，仅用于候选召回；实际采用前必须结合题目数据、假设、求解器与失败模式做二次审查。
