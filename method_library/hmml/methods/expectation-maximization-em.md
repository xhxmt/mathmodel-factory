# Expectation Maximization (EM)

> 来源：LLM-MM-Agent HMML；本文件由 `scripts/import_hmml.py` 确定性生成。

## 分层位置

Machine Learning (Machine Learning) → Clustering (Clustering)

## 建模方法

The EM algorithm optimizes parameter estimation by alternately executing the following two steps: Expectation step (E-step): Calculate the conditional expectation of the latent variables given the current parameter estimates, i.e., the expected value of the latent variables given the observed data. Maximization step (M-step): Maximize the likelihood function based on the expected values of the latent variables obtained in the E-step, updating the model parameters. These two steps alternate until the model parameters converge.

## 核心思想

上游 HMML 未单独标注。

## 典型应用

The EM algorithm is widely used in the following fields: Mixture model estimation: In Gaussian Mixture Models (GMM), the EM algorithm is used to estimate the parameters of each Gaussian distribution. Missing data imputation: In cases of missing data, the EM algorithm can estimate the missing values to complete the dataset. Clustering analysis: In unsupervised learning, the EM algorithm is used for clustering data, such as image segmentation in image processing. Hidden Markov Models (HMM): Used to estimate the parameters of HMMs, such as state transition probabilities and observation probabilities.

## 使用边界

该条目来自广覆盖方法目录，仅用于候选召回；实际采用前必须结合题目数据、假设、求解器与失败模式做二次审查。
