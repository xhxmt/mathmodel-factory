# Principal Component Analysis (PCA)

> 来源：LLM-MM-Agent HMML；本文件由 `scripts/import_hmml.py` 确定性生成。

## 分层位置

Machine Learning (Machine Learning) → Dimensionality Reduction (Dimensionality Reduction) → Linear Dimensionality Reduction (Linear Dimensionality Reduction)

## 建模方法

Principal Component Analysis (PCA) is a widely used data dimensionality reduction technique in statistics and machine learning, aiming to map high-dimensional data to a lower-dimensional space through linear transformation while retaining as much of the original data's variance as possible.

## 核心思想

The main goal of PCA is to find a set of new variables (principal components) that can explain the largest variance in the data. Specifically, PCA achieves dimensionality reduction through the following steps: 1. Data standardization: Standardize the original data so that each feature has a mean of 0 and a variance of 1 to eliminate the influence of different scales and magnitudes. 2. Compute the covariance matrix: Calculate the covariance matrix of the standardized data, reflecting the correlation between features. 3. Eigenvalue decomposition: Perform eigenvalue decomposition on the covariance matrix to obtain eigenvalues and corresponding eigenvectors. The eigenvalues represent the variance of the principal components, and the eigenvectors represent the directions of the principal components. 4. Select principal components: Select the top k principal components based on the magnitude of the eigenvalues, which can explain most of the variance in the data. 5. Construct a new feature space: Project the original data onto the selected principal components to obtain the reduced-dimensional data.

## 典型应用

PCA is widely used in the following fields: Data visualization: Reducing high-dimensional data to two or three dimensions for intuitive display and analysis. Noise reduction: Removing noise and redundant information from the data by retaining the main components. Feature selection: In machine learning, PCA is used to reduce the feature dimensions, lower model complexity, and improve computational efficiency. Image processing: In applications such as face recognition, PCA is used to extract the main features of images.

## 使用边界

该条目来自广覆盖方法目录，仅用于候选召回；实际采用前必须结合题目数据、假设、求解器与失败模式做二次审查。
