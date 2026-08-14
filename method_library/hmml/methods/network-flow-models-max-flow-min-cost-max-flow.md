# Network Flow Models (Max-Flow/Min-Cost Max-Flow)

> 来源：LLM-MM-Agent HMML；本文件由 `scripts/import_hmml.py` 确定性生成。

## 分层位置

Operations Research (Operations Research, OR) → Graph Theory (Graph Theory) → Flow (Flow)

## 建模方法

Network Flow Models are used in mathematical modeling to describe and optimize the allocation of flow in networks, widely applied in transportation, logistics, communication networks, and other fields.

## 核心思想

Max-Flow Problem: The Max-Flow problem aims to determine the maximum flow from a source to a sink in a flow network. The core idea is to find the maximum flow from the source to the sink while satisfying the capacity constraints of each edge. Common algorithms for solving this problem include the Edmonds-Karp algorithm and the Dinic algorithm. For example, in transportation, the Max-Flow model can help determine the maximum traffic capacity from a starting point to an endpoint in a road network. Min-Cost Max-Flow Problem: The Min-Cost Max-Flow problem extends the Max-Flow problem by introducing a cost per unit of flow on each edge, with the goal of minimizing the total cost while achieving the maximum flow. The core idea is to find a flow distribution scheme that minimizes the total transportation or circulation cost while satisfying the maximum flow condition. Common algorithms for solving this problem include the Shortest Path Faster Algorithm (SPFA) and the Successive Shortest Path Algorithm. For example, in logistics, the Min-Cost Max-Flow model can help determine the optimal delivery route that meets demand while minimizing transportation costs.

## 典型应用

Transportation: Optimize traffic flow distribution in road or transportation networks to reduce congestion and improve efficiency. Logistics: Plan optimal delivery routes to minimize transportation costs. Communication Networks: Optimize data transmission paths to enhance network bandwidth utilization. Power Systems: Optimize power flow to ensure stable operation of the power grid.

## 使用边界

该条目来自广覆盖方法目录，仅用于候选召回；实际采用前必须结合题目数据、假设、求解器与失败模式做二次审查。
