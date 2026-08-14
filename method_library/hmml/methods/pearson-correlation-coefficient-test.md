# Pearson Correlation Coefficient Test

> 来源：LLM-MM-Agent HMML；本文件由 `scripts/import_hmml.py` 确定性生成。

## 分层位置

Evaluation Methods (Evaluation Methods) → Statistical Evaluation (Statistical Evaluation) → Correlation Test (Correlation Test)

## 建模方法

The Pearson Correlation Coefficient Test is a statistical method used to measure the linear correlation between two continuous variables.

## 核心思想

The core idea is to evaluate the strength and direction of the linear relationship between two variables by calculating the Pearson correlation coefficient (\( r \)). The formula for the Pearson correlation coefficient is: \[ r = \frac{\sum (X_i - \overline{X})(Y_i - \overline{Y})}{\sqrt{\sum (X_i - \overline{X})^2 \sum (Y_i - \overline{Y})^2}} \] where \( X_i \) and \( Y_i \) are the \( i \)-th observations of variables \( X \) and \( Y \), respectively. \( \overline{X} \) and \( \overline{Y} \) are the means of variables \( X \) and \( Y \), respectively. The value of the Pearson correlation coefficient ranges from -1 to 1: \( r = 1 \): perfect positive correlation. \( r = -1 \): perfect negative correlation. \( r = 0 \): no linear correlation. Generally, the larger the absolute value of \( |r| \), the stronger the linear correlation.

## 典型应用

The Pearson Correlation Coefficient Test is widely used in various fields such as finance, medicine, and social sciences to evaluate the linear relationship between variables.

## 使用边界

该条目来自广覆盖方法目录，仅用于候选召回；实际采用前必须结合题目数据、假设、求解器与失败模式做二次审查。
