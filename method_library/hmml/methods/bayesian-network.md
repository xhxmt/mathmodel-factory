# Bayesian Network

> 来源：LLM-MM-Agent HMML；本文件由 `scripts/import_hmml.py` 确定性生成。

## 分层位置

Prediction (Prediction) → Discrete Prediction (Discrete Prediction)

## 建模方法

Bayesian Network, also known as belief network or directed acyclic graph model, is a probabilistic graphical model used to represent random variables and their conditional dependencies.

## 核心思想

Bayesian Network uses a directed acyclic graph (DAG) to represent causal relationships between random variables. Each node in the graph represents a random variable, and edges represent conditional dependencies between variables. Through local conditional probability distributions, Bayesian Network can effectively represent and reason about complex probabilistic relationships. The main components are: 1. Nodes: Represent random variables, which can be observable or latent variables. 2. Directed edges: Represent conditional dependencies between variables. 3. Conditional Probability Tables (CPTs): Define the probability distribution of each node given its parent nodes.

## 典型应用

Bayesian Network is widely used in the following fields: Medical diagnosis: Assists doctors in making diagnostic decisions by modeling the relationships between symptoms and diseases. Risk assessment: Evaluates the probability of system failures or financial crises in finance and engineering. Natural language processing: Handles uncertainties in language, such as speech recognition and machine translation. Machine learning: Used for classification, regression, and generative models, handling complex probabilistic reasoning problems.

## 使用边界

该条目来自广覆盖方法目录，仅用于候选召回；实际采用前必须结合题目数据、假设、求解器与失败模式做二次审查。
