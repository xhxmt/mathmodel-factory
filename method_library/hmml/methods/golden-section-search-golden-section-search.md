# Golden-Section Search (Golden-Section Search)

> 来源：LLM-MM-Agent HMML；本文件由 `scripts/import_hmml.py` 确定性生成。

## 分层位置

Optimization Methods (Optimization Methods) → Iterative Algorithm (Iterative Algorithms)

## 建模方法

The Golden-Section Search is an optimization algorithm used to find the extremum of a unimodal function within a one-dimensional interval. This method iteratively narrows the interval containing the extremum to gradually approach the optimal solution.

## 核心思想

The core idea of the Golden-Section Search is to select two points in each iteration to divide the current interval into three parts, where the ratio of one part's length to the entire interval's length is the golden ratio (approximately 0.618). This ensures that the length of the interval containing the extremum is reduced by the golden ratio in each iteration.

## 典型应用

The Golden-Section Search is widely used in the following types of mathematical modeling problems: Finding the extremum of a one-dimensional unimodal function: suitable for cases where the function has only one extremum point within a known interval. Engineering design optimization: used to optimize design parameters in engineering design where precise parameter adjustment is needed to achieve optimal performance. Hyperparameter tuning in machine learning: used to find the optimal hyperparameters during model training to achieve the best model performance.

## 使用边界

该条目来自广覆盖方法目录，仅用于候选召回；实际采用前必须结合题目数据、假设、求解器与失败模式做二次审查。
