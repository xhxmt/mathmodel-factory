# Entropy Weight Method

> 来源：LLM-MM-Agent HMML；本文件由 `scripts/import_hmml.py` 确定性生成。

## 分层位置

Evaluation Methods (Evaluation Methods) → Scoring Evaluation (Scoring Evaluation)

## 建模方法

The Entropy Weight Method is an objective weighting method based on information theory principles, used to determine the weights of indicators in multi-indicator comprehensive evaluation. Its core idea is to measure the information content of indicators based on their variability; the greater the information content, the higher the weight of the indicator.

## 核心思想

The basic steps of the Entropy Weight Method include: 1. Data standardization: Standardize the original data to eliminate the influence of dimensions. 2. Calculate the proportion: Calculate the proportion of each indicator under each alternative as the basis for entropy calculation. 3. Calculate the entropy value: Calculate the entropy value of each indicator based on the proportion, reflecting its uncertainty. 4. Calculate the difference coefficient: Calculate the difference coefficient of each indicator through the entropy value, measuring its variability. 5. Determine the weight: Determine the weight of each indicator based on the difference coefficient, with the weight proportional to the difference coefficient.

## 典型应用

The Entropy Weight Method is widely used in the following fields: Education evaluation: Evaluating the comprehensive performance of schools or classes, such as academic performance, discipline, and conduct. Enterprise management: Conducting comprehensive evaluations of employee performance, department performance, etc. Environmental evaluation: Evaluating environmental quality, pollution levels, and other indicators. Financial analysis: Evaluating the risks and returns of investment projects and financial products. Social surveys: Conducting comprehensive analyses of social phenomena and public opinion.

## 使用边界

该条目来自广覆盖方法目录，仅用于候选召回；实际采用前必须结合题目数据、假设、求解器与失败模式做二次审查。
