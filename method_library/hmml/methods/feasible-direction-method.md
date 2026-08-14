# Feasible Direction Method

> 来源：LLM-MM-Agent HMML；本文件由 `scripts/import_hmml.py` 确定性生成。

## 分层位置

Optimization Methods (Optimization Methods) → Constrained Optimization (Constrained Optimization)

## 建模方法

The Feasible Direction Method is an iterative algorithm used to solve constrained optimization problems. Its basic idea is to start from a feasible point and perform a one-dimensional search along the descent direction of the objective function, gradually approaching the optimal solution.

## 核心思想

Select Search Direction: At the current feasible point, determine a direction that decreases the objective function value, called the "descent feasible direction." Determine Step Size: Perform a line search along the selected descent feasible direction to find the step size that minimizes the objective function value. Update Iteration: Update the current point's position based on the calculated step size and repeat the process until the stopping criterion is met.

## 典型应用

The Feasible Direction Method is widely used in the following types of mathematical modeling problems: Constrained Optimization Problems: Suitable for cases where both the objective function and constraints are continuously differentiable. Engineering Design Optimization: Such as structural optimization and parameter tuning, finding the optimal solution that meets design requirements. Optimal Resource Allocation in Economics: Achieving profit maximization or cost minimization under limited resources.

## 使用边界

该条目来自广覆盖方法目录，仅用于候选召回；实际采用前必须结合题目数据、假设、求解器与失败模式做二次审查。
