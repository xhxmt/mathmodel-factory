# Hierarchical Clustering

> 来源：LLM-MM-Agent HMML；本文件由 `scripts/import_hmml.py` 确定性生成。

## 分层位置

Machine Learning (Machine Learning) → Clustering (Clustering)

## 建模方法

Hierarchical clustering constructs a clustering tree through the following two main strategies: Agglomerative: A bottom-up approach, where each data point is initially considered an independent cluster, and then the most similar clusters are gradually merged until a stopping condition is met. Divisive: A top-down approach, starting from the entire dataset and recursively dividing it into smaller clusters until a stopping condition is met. In practice, agglomerative hierarchical clustering is more common.

## 核心思想

上游 HMML 未单独标注。

## 典型应用

Hierarchical clustering is widely used in the following fields: Data visualization: Displaying the hierarchical structure of data through a dendrogram for intuitive understanding of data distribution and relationships. Gene expression analysis: In bioinformatics, hierarchical clustering is used to analyze gene expression data and identify functional modules of genes. Market segmentation: Dividing the market into different segments based on consumer behavior to develop targeted marketing strategies. Image processing: In image segmentation, hierarchical clustering is used to divide an image into different regions for subsequent analysis.

## 使用边界

该条目来自广覆盖方法目录，仅用于候选召回；实际采用前必须结合题目数据、假设、求解器与失败模式做二次审查。
