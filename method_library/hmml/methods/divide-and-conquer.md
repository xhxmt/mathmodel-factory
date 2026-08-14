# Divide and Conquer

> 来源：LLM-MM-Agent HMML；本文件由 `scripts/import_hmml.py` 确定性生成。

## 分层位置

Optimization Methods (Optimization Methods) → Deterministic Algorithms (Deterministic Algorithms)

## 建模方法

Divide and Conquer is an algorithm design paradigm that breaks a complex problem into smaller, similar subproblems, solves each subproblem independently, and then combines their solutions to solve the original problem.

## 核心思想

The core idea of Divide and Conquer is to decompose a complex problem into smaller, independent subproblems, recursively solve these subproblems, and then merge their solutions to obtain the solution to the original problem.

## 典型应用

Divide and Conquer is widely used in the following types of mathematical modeling problems: Sorting Problems: Algorithms like Merge Sort and Quick Sort decompose the original dataset, sort the subsets, and then merge them to achieve overall sorting. Matrix Multiplication: Strassen's algorithm reduces the number of multiplications by decomposing matrices, improving computational efficiency. Closest Pair of Points Problem: In a plane, finding the closest pair of points is solved by dividing the plane, recursively solving subproblems, and merging the results. Large Integer Multiplication: Karatsuba's algorithm decomposes large integers into smaller ones, recursively performs multiplication, and reduces computational complexity. Fast Fourier Transform (FFT): Used in signal processing, FFT decomposes the complex Fourier transform problem into smaller subproblems, recursively solves them, and merges the results.

## 使用边界

该条目来自广覆盖方法目录，仅用于候选召回；实际采用前必须结合题目数据、假设、求解器与失败模式做二次审查。
