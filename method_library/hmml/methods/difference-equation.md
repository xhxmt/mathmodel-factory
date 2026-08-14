# Difference Equation

> 来源：LLM-MM-Agent HMML；本文件由 `scripts/import_hmml.py` 确定性生成。

## 分层位置

Prediction (Prediction) → Discrete Prediction (Discrete Prediction)

## 建模方法

Difference Equation is a mathematical model that describes the behavior of discrete-time dynamic systems, similar to differential equations in continuous time, but its variables take values only at discrete time points.

## 核心思想

Difference Equation defines the relationship between each term of a sequence and the previous terms through a recursive relation, usually expressed as: \[ x_{n+1} = f(x_n, x_{n-1}, \dots, x_{n-k}) \] where \( x_n \) represents the state value at time step \( n \), \( f \) is the function describing the state change, and \( k \) is the order of the equation.

## 典型应用

Difference Equation is widely used in the following fields: Economics: Modeling the dynamic changes of economic indicators such as inflation rate and GDP growth rate. Biology: Describing population dynamics, disease spread models, etc. Engineering: Used in control systems to describe the behavior of discrete-time systems.

## 使用边界

该条目来自广覆盖方法目录，仅用于候选召回；实际采用前必须结合题目数据、假设、求解器与失败模式做二次审查。
