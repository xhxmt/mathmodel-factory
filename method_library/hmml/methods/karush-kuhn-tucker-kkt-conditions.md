# Karush-Kuhn-Tucker (KKT) Conditions

> 来源：LLM-MM-Agent HMML；本文件由 `scripts/import_hmml.py` 确定性生成。

## 分层位置

Optimization Methods (Optimization Methods) → Solving Techniques

## 建模方法

The KKT conditions are necessary conditions for solving constrained optimization problems, especially suitable for nonlinear programming problems with inequality constraints.

## 核心思想

The KKT conditions introduce Lagrange multipliers to combine constraints with the objective function, forming a Lagrangian function. By taking partial derivatives of the Lagrangian function and setting them to zero, a set of equations is obtained, and solving these equations yields the extrema of the original problem. The main components of the KKT conditions are: Primal feasibility: all inequality constraints must be satisfied. Dual feasibility: the Lagrange multipliers corresponding to inequality constraints must be non-negative. Complementary slackness: the product of each inequality constraint's Lagrange multiplier and the constraint's value is zero. Stationarity: the gradient of the objective function equals the linear combination of the gradients of the constraints.

## 典型应用

The KKT conditions are widely used in the following types of mathematical modeling problems: Nonlinear programming problems: especially optimization problems with inequality constraints. Support vector machines (SVM): in machine learning, the training process of SVM can be solved using the KKT conditions. Optimal resource allocation in economics: achieving maximum benefit under limited resources.

## 使用边界

该条目来自广覆盖方法目录，仅用于候选召回；实际采用前必须结合题目数据、假设、求解器与失败模式做二次审查。
