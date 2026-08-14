# Local Linear Embedding (LLE)

> 来源：LLM-MM-Agent HMML；本文件由 `scripts/import_hmml.py` 确定性生成。

## 分层位置

Machine Learning (Machine Learning) → Dimensionality Reduction (Dimensionality Reduction) → Nonlinear Dimensionality Reduction (Nonlinear Dimensionality Reduction)

## 建模方法

Local Linear Embedding (LLE) is a non-linear dimensionality reduction method that aims to reveal the low-dimensional manifold structure of high-dimensional data by preserving local linear relationships.

## 核心思想

The basic idea of LLE is to assume that data has a linear structure in local neighborhoods, meaning each data point can be reconstructed by a linear combination of its neighbors. During the dimensionality reduction process, LLE achieves this through the following steps: 1. Neighborhood construction: For each data point, determine its k nearest neighbors. 2. Weight calculation: Within the neighborhood of each data point, calculate reconstruction weights such that the point can be reconstructed by a linear combination of its neighbors. 3. Dimensionality reduction mapping: In the low-dimensional space, find a mapping such that the reduced data points can still be reconstructed by the same linear combination of weights, thereby preserving the local structure.

## 典型应用

LLE is widely used in the following fields: High-dimensional data visualization: Reducing high-dimensional data to two or three dimensions for intuitive display and analysis. Image processing: In applications such as face recognition, LLE is used to extract the main features of images. Manifold learning: Revealing the intrinsic low-dimensional structure of data to help understand the underlying characteristics of the data.

## 使用边界

该条目来自广覆盖方法目录，仅用于候选召回；实际采用前必须结合题目数据、假设、求解器与失败模式做二次审查。
