# Laplacian Eigenmaps

> 来源：LLM-MM-Agent HMML；本文件由 `scripts/import_hmml.py` 确定性生成。

## 分层位置

Machine Learning (Machine Learning) → Dimensionality Reduction (Dimensionality Reduction) → Nonlinear Dimensionality Reduction (Nonlinear Dimensionality Reduction)

## 建模方法

Laplacian Eigenmaps (LE) is a non-linear dimensionality reduction method that aims to map high-dimensional data to a low-dimensional space by preserving the local geometric structure of the data.

## 核心思想

The basic idea of LE is that if data points are close to each other in the high-dimensional space (i.e., similar in the local neighborhood), they should also remain close in the reduced low-dimensional space. Specifically, LE achieves dimensionality reduction through the following steps: 1. Constructing an adjacency graph: Based on the similarity between data points, construct an undirected weighted graph where each node represents a data point, and the edge weights represent the similarity between data points. 2. Calculating the Laplacian matrix: Compute the Laplacian matrix from the adjacency graph, which is the difference between the degree matrix and the adjacency matrix of the graph. 3. Eigenvalue decomposition: Perform eigenvalue decomposition on the Laplacian matrix, selecting the first k smallest eigenvalues and their corresponding eigenvectors to form the basis of the low-dimensional space. 4. Mapping to the low-dimensional space: Map the original data points to the low-dimensional space formed by the selected eigenvectors, completing the dimensionality reduction.

## 典型应用

上游 HMML 未单独标注。

## 使用边界

该条目来自广覆盖方法目录，仅用于候选召回；实际采用前必须结合题目数据、假设、求解器与失败模式做二次审查。
