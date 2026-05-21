# 混合整数线性规划 (MILP)

> Mixed-Integer Linear Programming

## 适用场景

- 决策变量含整数（特别是 0-1 变量）的优化问题
- 目标和约束**都可写成线性**或可线性化（用大-M、转换变量等技巧）
- 经典模式：**指派 / 选址 / 调度 / 网络流 / 切割与下料 / 生产计划 / 路径选择**
- CUMCM 决策类、规划类、运筹类题目的"主力武器"

不适用：
- 目标或约束含本质非线性（乘积、除法、指数、sin/cos）—— 用 MINLP 或转化（McCormick 包络、分段线性化）
- 决策变量纯连续 → 普通 LP
- 决策变量纯整数且规模小 → 可考虑动态规划
- 变量 ≥ 10⁵ 且时间敏感 → 考虑启发式（GA / 模拟退火 / 列生成）

## 核心假设

1. 决策可被离散变量 + 连续变量描述
2. 目标线性，约束线性
3. 求解器在合理时间内能找到（近似）最优 —— 整数程序 NP-hard，规模大时必须设 `--max-time` + MIP gap 容忍

## 数学形式

$$
\begin{aligned}
\min_{\mathbf{x}} \quad & \mathbf{c}^{\top} \mathbf{x} \\
\text{s.t.} \quad & A \mathbf{x} \le \mathbf{b}, \\
& \mathbf{l} \le \mathbf{x} \le \mathbf{u}, \\
& x_j \in \mathbb{Z}, \quad j \in J \\
& x_j \in \{0, 1\}, \quad j \in B
\end{aligned}
$$

经典约束模式：
- **指派**：$\sum_j x_{ij} = 1, \forall i$
- **容量**：$\sum_i a_i x_{ij} \le C_j, \forall j$
- **逻辑联结（if-then）**：$x \le M y$，$y \in \{0,1\}$（"$y=0 \Rightarrow x=0$"）
- **固定成本**：成本 $= f y + c x$，要求 $x \le M y$
- **互斥选项**：$\sum_{k\in K} y_k = 1$（"$|K|$ 选 1"）

## 求解工具

| 工具 | 接口 | 何时用 |
| --- | --- | --- |
| **Gurobi** (`gurobipy`) | Python 原生 | 首选（速度最快，国赛有学术 license） |
| **CPLEX** (`docplex`) | Python | Gurobi 的替代品 |
| **PuLP** | Python | 写起来简单，底层默认 CBC（开源，比 Gurobi 慢 10-100×） |
| **CVXPY** | Python | 表达力强，但 MIP 性能依赖底层求解器 |
| **OR-Tools** (`pywraplp`) | Python | 谷歌出品，CP-SAT 求解器在调度类问题上常常打败 Gurobi |
| `scipy.optimize.milp` | Python (scipy ≥ 1.9) | 中小规模可用，单变量类型支持有限 |

**国赛推荐**：`gurobipy` 主力，`scipy.optimize.milp` 作为无 license 时的兜底。

## 代码模板

```python
"""
milp_template.py — 用 gurobipy 解 0-1 背包 (示例：可替换为题目实际约束)
用法:
    pip install gurobipy
    python milp_template.py
"""
import numpy as np
import gurobipy as gp
from gurobipy import GRB

np.random.seed(42)  # 固定随机种子, 满足 modeling_guide.md 复现要求

# ---- 数据 ----
n = 20
values  = np.random.randint(10, 100, size=n)
weights = np.random.randint(5,  40, size=n)
capacity = int(0.4 * weights.sum())

# ---- 模型 ----
m = gp.Model("knapsack")
m.setParam("OutputFlag", 1)
m.setParam("TimeLimit", 60)        # max-time, 严格控制
m.setParam("MIPGap", 0.01)         # 1% gap 即可

x = m.addVars(n, vtype=GRB.BINARY, name="x")
m.addConstr(gp.quicksum(weights[i] * x[i] for i in range(n)) <= capacity,
            name="capacity")
m.setObjective(gp.quicksum(values[i] * x[i] for i in range(n)), GRB.MAXIMIZE)

m.optimize()

# ---- 结果 ----
if m.Status in (GRB.OPTIMAL, GRB.TIME_LIMIT, GRB.SUBOPTIMAL):
    chosen = [i for i in range(n) if x[i].X > 0.5]
    print(f"选中物品: {chosen}")
    print(f"总价值: {m.ObjVal:.0f}  上界 (BestBd): {m.ObjBound:.0f}  gap: {m.MIPGap*100:.2f}%")
    print(f"求解耗时: {m.Runtime:.2f}s")
else:
    print(f"求解失败, status={m.Status}")
```

**无 Gurobi 时的 scipy 版本**：
```python
from scipy.optimize import milp, LinearConstraint, Bounds
import numpy as np

c = -values  # scipy 默认最小化
A = weights.reshape(1, -1)
constraints = LinearConstraint(A, ub=capacity)
integrality = np.ones(n)  # 全部整数
bounds = Bounds(lb=0, ub=1)

res = milp(c, constraints=constraints, integrality=integrality, bounds=bounds,
           options={"time_limit": 60})
print(f"目标 = {-res.fun:.0f}, 选中 = {np.where(res.x > 0.5)[0].tolist()}")
```

## 常见陷阱

1. **大 M 取值**
   - 过大 → LP 松弛松，求解慢，数值不稳
   - 过小 → 可行解被切掉，结果错误
   - 经验：取约束右端项的紧致上界，**不要取 10⁶ 这种保守值**
2. **目标 / 约束的非线性偷渡**：写 $x \cdot y$（连续 × 连续）就不是 MILP 了；二者均 0-1 时可用 $z = x \wedge y$ 线性化：$z \le x, z \le y, z \ge x+y-1$
3. **未设时间限制**：复杂题目可能跑几小时不收敛。**必须** `solver_submit.sh --max-time` 或 Gurobi `TimeLimit`
4. **MIPGap 不设容忍**：默认 0.0001 在大规模问题上几乎跑不完；国赛建议 0.5%–2%
5. **整数被求解器当连续返回**：检查 `vtype` 设置；输出时用 `round()` 或阈值 0.5
6. **多解性 (degeneracy)**：可能有多个等价最优解，对论文叙述不利。可加扰动项 $\epsilon \sum x_i$ 让最优解唯一
7. **infeasible / unbounded**：先用 `m.computeIIS()` 找不可行子集，逐个排查
8. **未导出灵敏度信息**：MIP 没有 LP 那样的影子价格；要做灵敏度，需对参数手动扰动 + 多次求解（CUMCM 评分会看）

## 在建模比赛中的典型应用

- **生产决策类**（次品检测、批次决策、流水线优化）—— 经典 0-1 + 阶段决策
- **指派 / 选址 / 调度**（机器作业、人员排班、配送中心选址）
- **网络优化**（最大流、最小费用流，整数路径）
- **多目标**：通过加权或字典序转 MILP
- 真实 CUMCM 案例：2024 国赛 B 题（生产过程决策）即 0-1 阶段决策 + 期望成本最小化，几乎必须用 MILP 或决策树枚举

## 参考文献

- Wolsey, L. A. (2020). *Integer Programming* (2nd ed.). Wiley.
- Gurobi Optimization. *Gurobi Optimizer Reference Manual*. https://docs.gurobi.com/
- Bertsimas, D., & Tsitsiklis, J. N. (1997). *Introduction to Linear Optimization*. Athena Scientific.
- 司守奎 & 孙兆亮 (2015). 《数学建模算法与应用》(第二版). 第 4–6 章.
