# Analysis of Variance (ANOVA)

> 来源：LLM-MM-Agent HMML；本文件由 `scripts/import_hmml.py` 确定性生成。

## 分层位置

Evaluation Methods (Evaluation Methods) → Goodness of Fit Test (Goodness of Fit Test)

## 建模方法

Analysis of Variance (ANOVA) is a statistical method used to test whether there are significant differences in the means of three or more groups.

## 核心思想

The core idea is to compare the variability between groups and within groups to determine if the differences between groups exceed random error. The basic steps include: 1. Hypothesis testing: Null hypothesis (\( H_0 \)): All group means are equal. Alternative hypothesis (\( H_1 \)): At least one group mean is different. 2. Calculate variability: Within-group variability (\( SS_{\text{Error}} \)): Measures differences within groups. Between-group variability (\( SS_{\text{Treatments}} \)): Measures differences between group means and the overall mean. 3. Calculate mean squares (MS): Within-group mean square (\( MS_{\text{Error}} \)): \( MS_{\text{Error}} = \frac{SS_{\text{Error}}}{df_{\text{Error}}} \). Between-group mean square (\( MS_{\text{Treatments}} \)): \( MS_{\text{Treatments}} = \frac{SS_{\text{Treatments}}}{df_{\text{Treatments}}} \). 4. Calculate F-statistic: \( F = \frac{MS_{\text{Treatments}}}{MS_{\text{Error}}} \). 5. Significance test: Compare the calculated F-value with the critical value from the F-distribution table to determine if the null hypothesis should be rejected.

## 典型应用

ANOVA is widely used in fields such as medical research, education evaluation, market research, agricultural science, and psychological experiments to compare the effects of different treatments or conditions.

## 使用边界

该条目来自广覆盖方法目录，仅用于候选召回；实际采用前必须结合题目数据、假设、求解器与失败模式做二次审查。
