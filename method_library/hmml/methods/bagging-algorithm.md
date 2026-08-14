# Bagging Algorithm

> 来源：LLM-MM-Agent HMML；本文件由 `scripts/import_hmml.py` 确定性生成。

## 分层位置

Machine Learning (Machine Learning) → Ensemble Learning Algorithms (Ensemble Learning Algorithms)

## 建模方法

Bagging (Bootstrap Aggregating) is an ensemble learning method aimed at improving the accuracy and stability of a model by combining the predictions of multiple models.

## 核心思想

The basic idea of Bagging is to generate multiple different subsets of the training set through random sampling with replacement, train multiple base learners on these subsets, and then combine the predictions of these base learners to obtain the final prediction. Specifically, the steps of the Bagging algorithm are as follows: 1. Data sampling: Randomly draw multiple subsets from the original training set using sampling with replacement. Each subset is typically the same size as the original training set, but due to sampling with replacement, some samples may appear multiple times in the same subset, while others may not appear at all. 2. Model training: Train a base learner on each subset. 3. Combine results: For classification problems, use voting, where the class with the most predictions from the base learners is chosen as the final prediction; for regression problems, use averaging, where the average of the base learners' predictions is taken as the final prediction.

## 典型应用

Bagging algorithms are particularly suitable for the following situations: High variance models: For high variance models that are prone to overfitting (such as decision trees), Bagging can effectively reduce the variance and improve generalization ability. Noisy data: In the presence of noisy data, Bagging can reduce the impact of noise on the model through multiple training and result combination.

## 使用边界

该条目来自广覆盖方法目录，仅用于候选召回；实际采用前必须结合题目数据、假设、求解器与失败模式做二次审查。
