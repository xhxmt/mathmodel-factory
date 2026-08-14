# Chi-Square Goodness-of-Fit Test

> 来源：LLM-MM-Agent HMML；本文件由 `scripts/import_hmml.py` 确定性生成。

## 分层位置

Evaluation Methods (Evaluation Methods) → Goodness of Fit Test (Goodness of Fit Test)

## 建模方法

The Chi-Square Goodness-of-Fit Test is a statistical method used to test whether observed data fits a specified distribution or proportion.

## 核心思想

The core idea is to compare the observed frequencies with the expected frequencies to evaluate if the data matches the hypothesized distribution. The basic steps include: 1. Hypothesis testing: Null hypothesis (\( H_0 \)): The observed data fits the expected distribution. Alternative hypothesis (\( H_1 \)): The observed data does not fit the expected distribution. 2. Calculate expected frequencies: Based on the expected distribution or proportion. 3. Calculate chi-square statistic: Using the formula: \[ \chi^2 = \sum \frac{(O_i - E_i)^2}{E_i} \] where \( O_i \) is the observed frequency for category \( i \), and \( E_i \) is the expected frequency for category \( i \). 4. Determine degrees of freedom: Usually the number of categories minus one, \( df = k - 1 \), where \( k \) is the number of categories. 5. Find critical value or calculate p-value: Based on the chi-square statistic and degrees of freedom. 6. Make a decision: If the chi-square statistic is greater than the critical value or the p-value is less than the significance level (e.g., 0.05), reject the null hypothesis.

## 典型应用

The Chi-Square Goodness-of-Fit Test is widely used in market research, education evaluation, biostatistics, and social sciences to evaluate if observed data matches expected distributions.

## 使用边界

该条目来自广覆盖方法目录，仅用于候选召回；实际采用前必须结合题目数据、假设、求解器与失败模式做二次审查。
