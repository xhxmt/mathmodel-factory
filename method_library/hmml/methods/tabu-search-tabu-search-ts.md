# Tabu Search (Tabu Search, TS)

> 来源：LLM-MM-Agent HMML；本文件由 `scripts/import_hmml.py` 确定性生成。

## 分层位置

Optimization Methods (Optimization Methods) → Heuristic Algorithm (Heuristic Algorithms)

## 建模方法

Tabu Search (TS) is a global neighborhood search algorithm designed to avoid local optima by introducing a memory mechanism, thereby more effectively finding the global optimum.

## 核心思想

The core idea of Tabu Search is to use a tabu list to record visited solutions or moves, preventing the search process from revisiting the same solutions or falling into cycles. By setting tabu criteria, it allows accepting worse solutions under certain conditions to escape local optima and explore a broader solution space.

## 典型应用

Tabu Search is widely used in the following types of mathematical modeling problems: Combinatorial optimization problems: such as the Traveling Salesman Problem (TSP), knapsack problem, and scheduling problems. Function optimization problems: In high-dimensional complex function optimization, Tabu Search can effectively avoid local optima. Engineering design problems: such as structural optimization and parameter tuning, where Tabu Search can be used to find optimal design solutions. Hyperparameter optimization in machine learning: In model training, Tabu Search can be used to optimize hyperparameter configurations.

## 使用边界

该条目来自广覆盖方法目录，仅用于候选召回；实际采用前必须结合题目数据、假设、求解器与失败模式做二次审查。
