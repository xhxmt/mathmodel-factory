# 图论与网络流 (Graph Algorithms & Network Flow)

## 适用场景

- 路径规划 / 导航 / 最短路（最短路算法）
- 管网 / 交通 / 通信的流量上限与瓶颈（最大流）
- 带成本的运输 / 调度 / 供需匹配（最小费用最大流）
- 任务分配 / 指派（二分图匹配）
- 网络连通成本最小化（最小生成树）
- 关键节点 / 社区 / 连通性分析（中心性、连通分量）

不适用：
- 纯连续优化无网络结构 → LP / NLP
- 节点 / 边带复杂逻辑约束 → 建模成 MILP（网络流是其特例，复杂约束回归 MILP 更灵活）
- 超大规模（亿级边）实时 → 需专用图数据库 / 近似算法

## 核心假设

1. 问题可抽象为**点 + 边**结构（实体 = 点，关系 / 通道 = 边）
2. 最短路（Dijkstra）要求**边权非负**（有负权用 Bellman-Ford；有负环则无解）
3. 最大流 / 最小费用流：边有**容量**，流量守恒（除源汇）
4. 二分匹配：节点可划分为两个互不相交集合，边只跨集合

## 经典问题与算法

| 问题 | 算法 | 复杂度 | networkx API |
|---|---|---|---|
| 单源最短路（非负） | Dijkstra | O(E log V) | `shortest_path(weight=...)` |
| 最短路（含负权） | Bellman-Ford | O(VE) | `bellman_ford_path` |
| 最大流 | Dinic / Edmonds-Karp | O(V²E) | `maximum_flow` |
| 最小费用最大流 | SSP / 网络单纯形 | 多项式 | `max_flow_min_cost` |
| 二分图最大匹配 | Hopcroft-Karp | O(E√V) | `bipartite.maximum_matching` |
| 最小生成树 | Kruskal / Prim | O(E log V) | `minimum_spanning_tree` |

## 数学形式

**最大流-最小割定理**：最大流值 = 最小割容量。

**最小费用最大流**（在最大流约束下最小化成本）：
$$\min \sum_{(i,j)\in E} c_{ij} f_{ij} \quad \text{s.t.}\ \ 0\le f_{ij}\le u_{ij},\ \ \sum_j f_{ij}-\sum_k f_{ki}=b_i$$
其中 $b_i$ 为节点净供给（源 > 0、汇 < 0、中转 = 0）。

> 关键洞察：网络流问题都是**特殊结构的 LP**，约束矩阵全单位模 → LP 松弛即得整数最优解。约束变复杂（如分组上限、固定费用）时改用 MILP。

## 求解工具

- `networkx`：最完整，上述全部算法现成（`pip install networkx`）
- `scipy.sparse.csgraph`：`dijkstra / bellman_ford / min_weight_full_bipartite_matching`（大稀疏图更快）
- `scipy.optimize.linear_sum_assignment`：指派问题（匈牙利算法，方阵最优）
- 复杂约束 → 退回 `gurobipy` / `pulp` 建 MILP（见 [milp 方法]）

## 代码模板

```python
"""
graph_network_template.py — 图论/网络流五大经典问题
用法: python graph_network_template.py  (依赖 networkx)
"""
import networkx as nx

# ---- 1. 最短路 (Dijkstra, 边权非负) ----
G = nx.DiGraph()
G.add_weighted_edges_from([("A", "B", 4), ("A", "C", 2), ("C", "B", 1),
                           ("B", "D", 5), ("C", "D", 8)])
print("最短路 A->D:", nx.shortest_path(G, "A", "D", weight="weight"),
      "| 距离 =", nx.shortest_path_length(G, "A", "D", weight="weight"))
# 含负权改用: nx.bellman_ford_path(G, "A", "D")

# ---- 2. 最大流 (Dinic) ----
F = nx.DiGraph()
F.add_edge("s", "a", capacity=10); F.add_edge("s", "b", capacity=5)
F.add_edge("a", "b", capacity=15); F.add_edge("a", "t", capacity=10)
F.add_edge("b", "t", capacity=10)
flow_val, _ = nx.maximum_flow(F, "s", "t")
cut_value, (S, T) = nx.minimum_cut(F, "s", "t")
print(f"最大流 s->t = {flow_val}  (= 最小割容量 {cut_value}, 验证最大流-最小割定理)")

# ---- 3. 最小费用最大流 (带成本的运输/调度) ----
M = nx.DiGraph()
M.add_edge("s", "a", capacity=4, weight=2); M.add_edge("s", "b", capacity=2, weight=1)
M.add_edge("a", "t", capacity=3, weight=1); M.add_edge("b", "t", capacity=5, weight=3)
fd = nx.max_flow_min_cost(M, "s", "t")
print(f"最小费用最大流: 流量={nx.maximum_flow_value(M, 's', 't')} "
      f"总费用={nx.cost_of_flow(M, fd)}")

# ---- 4. 二分图最大匹配 (任务指派) ----
B = nx.Graph()
workers = {"w1", "w2", "w3"}
B.add_nodes_from(workers, bipartite=0)
B.add_edges_from([("w1", "j1"), ("w1", "j2"), ("w2", "j1"), ("w3", "j3")])
match = nx.bipartite.maximum_matching(B, top_nodes=workers)
print("最大匹配:", {k: v for k, v in match.items() if k in workers})

# ---- 5. 最小生成树 (网络连通最小成本) ----
U = nx.Graph()
U.add_weighted_edges_from([("A", "B", 1), ("B", "C", 2), ("A", "C", 3), ("C", "D", 4)])
mst = nx.minimum_spanning_tree(U)
print("MST 边:", list(mst.edges(data="weight")),
      "| 总权 =", sum(d for *_, d in mst.edges(data="weight")))
```

## 常见陷阱

1. **Dijkstra 用于负权图**：负权必须 Bellman-Ford；有负环则最短路无定义，要先检测。
2. **有向 / 无向混淆**：单行道、单向流量必须 `DiGraph`；用错图类型结果全错。
3. **容量 / 成本属性名写错**：networkx 默认认 `capacity` / `weight`，自定义名要显式传参。
4. **把复杂约束硬塞网络流**：分组上限、固定开启成本、互斥等用纯网络流表达不了，应建 MILP。
5. **大图用邻接矩阵**：稠密矩阵在大图上爆内存，用邻接表 / `scipy.sparse`。
6. **匹配问题误用最短路**：指派 / 匹配是独立问题族，用匈牙利 / 二分匹配，不是最短路。
7. **多源多汇不加超级源汇**：多个源 / 汇要建虚拟超级源 S、超级汇 T 再跑单源单汇算法。

## 在建模比赛中的典型应用

- 物流 / 交通 / 管网的路径与流量优化
- 资源 / 人员 / 任务的指派与匹配
- 供应链最小成本运输（最小费用流 = 运输问题）
- 关键基础设施识别（最小割 = 瓶颈 / 攻击面）
- 真实案例：CUMCM 涉及交通网络、应急调度、管网设计的题目常以网络流 / 最短路为核心或子模块

## 参考文献

- Ahuja, R. K., Magnanti, T. L., & Orlin, J. B. (1993). *Network Flows: Theory, Algorithms, and Applications*. Prentice Hall.
- Cormen, T. H., et al. (2009). *Introduction to Algorithms* (3rd ed.), Part VI. MIT Press.
- networkx documentation: https://networkx.org/documentation/stable/reference/algorithms/index.html
- 司守奎 & 孙兆亮 (2015). 《数学建模算法与应用》(第二版). 图论模型章节.
