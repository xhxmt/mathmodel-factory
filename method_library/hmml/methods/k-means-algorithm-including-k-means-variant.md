# K-Means Algorithm (including K-Means++ variant)

> 来源：LLM-MM-Agent HMML；本文件由 `scripts/import_hmml.py` 确定性生成。

## 分层位置

Machine Learning (Machine Learning) → Clustering (Clustering)

## 建模方法

The basic steps of the K-Means algorithm include: Initialization: Randomly select K data points as initial cluster centers (centroids). Assignment: Assign each data point to the nearest cluster center. Update: Recalculate the centroid of each cluster, which is the mean of all data points in the cluster. Iteration: Repeat steps 2 and 3 until the cluster centers no longer change or change very little, and the algorithm converges. Note that the K-Means algorithm is sensitive to the choice of initial cluster centers, which may lead to different results. K-Means++ variant: To address the sensitivity of K-Means to the choice of initial cluster centers, the K-Means++ algorithm was introduced. K-Means++ improves the initialization process by: Selecting the first cluster center: Randomly select a data point from the dataset as the first cluster center. Selecting subsequent cluster centers: For each data point not yet chosen as a cluster center, calculate the squared distance to the nearest chosen cluster center, and select the next cluster center based on these distances' probability distribution. Repeat: Repeat step 2 until K cluster centers are chosen. This method reduces the dependency on the initial cluster center selection, improving the stability and quality of clustering results.

## 核心思想

上游 HMML 未单独标注。

## 典型应用

K-Means and its variants are widely used in the following fields: Market segmentation: Dividing the market into different segments based on consumer behavior. Image compression: Compressing images by grouping similar-colored pixels. Document clustering: Grouping documents with similar topics for information retrieval. Gene data analysis: Clustering gene expression data in bioinformatics to discover functional modules of genes.

## 使用边界

该条目来自广覆盖方法目录，仅用于候选召回；实际采用前必须结合题目数据、假设、求解器与失败模式做二次审查。
