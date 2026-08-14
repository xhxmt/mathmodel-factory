# Kolmogorov-Smirnov Test (KS Test)

> 来源：LLM-MM-Agent HMML；本文件由 `scripts/import_hmml.py` 确定性生成。

## 分层位置

Evaluation Methods (Evaluation Methods) → Goodness of Fit Test (Goodness of Fit Test)

## 建模方法

The Kolmogorov-Smirnov Test (KS Test) is a non-parametric statistical method used to test whether a sample comes from a specified probability distribution or to compare if two samples come from the same distribution.

## 核心思想

The core idea is to compare the empirical cumulative distribution function (ECDF) of the sample with the theoretical cumulative distribution function (CDF) or between two samples' ECDFs to evaluate the degree of match. 1. One-sample KS Test: Null hypothesis (\( H_0 \)): The sample data fits the specified theoretical distribution. Alternative hypothesis (\( H_1 \)): The sample data does not fit the specified theoretical distribution. Calculate the test statistic: Compute the maximum absolute difference between the sample's ECDF and the theoretical CDF, denoted as \( D \). Significance test: Based on the \( D \) value and sample size, find the critical value or calculate the p-value to determine if the null hypothesis should be rejected. 2. Two-sample KS Test: Null hypothesis (\( H_0 \)): The two samples come from the same distribution. Alternative hypothesis (\( H_1 \)): The two samples come from different distributions. Calculate the test statistic: Compute the maximum absolute difference between the two samples' ECDFs, denoted as \( D \). Significance test: Based on the \( D \) value and sample sizes, find the critical value or calculate the p-value to determine if the null hypothesis should be rejected.

## 典型应用

The KS Test is widely used in statistical modeling, quality control, financial analysis, and biostatistics to evaluate if data fits a specified distribution or to compare distributions between samples.

## 使用边界

该条目来自广覆盖方法目录，仅用于候选召回；实际采用前必须结合题目数据、假设、求解器与失败模式做二次审查。
