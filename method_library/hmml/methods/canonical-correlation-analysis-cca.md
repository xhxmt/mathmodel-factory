# Canonical Correlation Analysis (CCA)

> 来源：LLM-MM-Agent HMML；本文件由 `scripts/import_hmml.py` 确定性生成。

## 分层位置

Machine Learning (Machine Learning) → Dimensionality Reduction (Dimensionality Reduction) → Linear Dimensionality Reduction (Linear Dimensionality Reduction)

## 建模方法

Canonical Correlation Analysis (CCA) is a multivariate statistical method used to study the correlation between two sets of variables.

## 核心思想

The basic idea of CCA is to find a set of linear combinations in each group of variables that maximize the correlation coefficient between these two sets of linear combinations. Specifically, given two sets of variables \( X = (x_1, x_2, \dots, x_p) \) and \( Y = (y_1, y_2, \dots, y_q) \), CCA seeks to solve for the linear combinations \( U = a^T X \) and \( V = b^T Y \) such that the correlation coefficient between \( U \) and \( V \) is maximized. This process is similar to Principal Component Analysis but focuses on the correlation between two sets of variables rather than the variance explained by a single set of variables.

## 典型应用

CCA is widely used in the following fields: Multivariate statistical analysis: Used to study the intrinsic relationship between two sets of variables, such as evaluating the consistency of different measurement tools for the same trait in psychology. Biostatistics: Analyzing the relationship between gene expression data and clinical features to reveal the association between biomarkers and diseases. Economics: Studying the mutual influence between macroeconomic indicators and microeconomic behaviors. Social sciences: Exploring the correlation between socioeconomic factors and educational outcomes.

## 使用边界

该条目来自广覆盖方法目录，仅用于候选召回；实际采用前必须结合题目数据、假设、求解器与失败模式做二次审查。
