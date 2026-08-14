# Newton Method/Quasi-Newton Methods (Newton Method)

> 来源：LLM-MM-Agent HMML；本文件由 `scripts/import_hmml.py` 确定性生成。

## 分层位置

Optimization Methods (Optimization Methods) → Iterative Algorithm (Iterative Algorithms)

## 建模方法

Newton's Method and Quasi-Newton Methods are commonly used algorithms for solving unconstrained optimization problems, known for their fast convergence speed.

## 核心思想

Newton's Method: By utilizing the gradient and Hessian matrix of the objective function, it calculates the search direction and step size in each iteration to quickly approach the optimal solution. Quasi-Newton Methods: To overcome the complexity of computing the inverse of the Hessian matrix in Newton's Method, Quasi-Newton Methods construct a positive definite matrix to approximate the inverse of the Hessian matrix, simplifying the computation process.

## 典型应用

These methods are widely used in the following types of mathematical modeling problems: Unconstrained optimization problems: such as function minimization and parameter estimation. Model training in machine learning: such as the training process of Support Vector Machines (SVM). Engineering design optimization: such as structural optimization and control system design.

## 使用边界

该条目来自广覆盖方法目录，仅用于候选召回；实际采用前必须结合题目数据、假设、求解器与失败模式做二次审查。
