# Greedy Algorithm

> 来源：LLM-MM-Agent HMML；本文件由 `scripts/import_hmml.py` 确定性生成。

## 分层位置

Optimization Methods (Optimization Methods) → Deterministic Algorithms (Deterministic Algorithms)

## 建模方法

The Greedy Algorithm is a method that makes the locally optimal choice at each step with the hope of finding the global optimum.

## 核心思想

The core idea of the Greedy Algorithm is to choose the best option available at each step without considering the future consequences. This strategy is suitable for problems that exhibit the "greedy choice property" and "optimal substructure," meaning that local optimal solutions lead to a global optimal solution.

## 典型应用

The Greedy Algorithm is widely used in the following types of mathematical modeling problems: Minimum Spanning Tree Problem: Algorithms like Prim's and Kruskal's are used to find the minimum spanning tree in a weighted undirected graph. Single-Source Shortest Path Problem: Dijkstra's algorithm is used to find the shortest path from a source to all other vertices in a weighted directed graph. Activity Selection Problem: Given a set of activities with start and end times, the goal is to select the maximum number of non-overlapping activities. Huffman Coding: Used for data compression by constructing an optimal prefix code tree to minimize the encoding length. Knapsack Problem: In the 0-1 knapsack problem, items are selected to maximize total value; in the fractional knapsack problem, items can be divided to maximize total value.

## 使用边界

该条目来自广覆盖方法目录，仅用于候选召回；实际采用前必须结合题目数据、假设、求解器与失败模式做二次审查。
