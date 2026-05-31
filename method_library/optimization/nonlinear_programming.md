# 非线性规划 (Nonlinear Programming, NLP)

## 适用场景

- 连续变量的**约束最优化**：$\min f(\mathbf x)$ s.t. 等式 / 不等式 / 边界约束，且 $f$ 或约束**非线性**
- 含三角函数、相切、几何距离等非线性关系的优化（无法线性化为 MILP，或线性化太粗）
- 参数标定 / 最小二乘拟合（无约束或带边界）
- 曲线缩短、半径 / 切点优化、限速反求等连续几何优化

不适用：
- 目标与约束**全线性**、含整数 / 0-1 变量 → `optimization/milp.md`
- 只求 $f(x)=0$ 的根，不求极值 → `numerical/root_finding.md`
- 高维多峰 / 不可微 / 组合 → `metaheuristic/*`（或先全局再 NLP 精修）
- 多目标 Pareto 前沿 → NSGA-II 类（method_library 暂未登记，必要时新增）

## 核心假设

1. 决策变量连续；$f$、约束在可行域内（分段）光滑或至少可数值求导
2. 约束可写成 $g_i(\mathbf x)\le0$、$h_j(\mathbf x)=0$、$\text{lb}\le\mathbf x\le\text{ub}$
3. 局部最优可接受，或通过多起点 / 全局法逼近全局最优
4. 问题规模适中（梯度由有限差分即可；大规模需解析梯度）

## 数学形式

$$\min_{\mathbf x}\ f(\mathbf x)\quad\text{s.t.}\quad g_i(\mathbf x)\le0,\ \ h_j(\mathbf x)=0,\ \ \mathrm{lb}\le \mathbf x\le \mathrm{ub}.$$

KKT 必要条件（正则点）：$\nabla f+\sum_i\mu_i\nabla g_i+\sum_j\lambda_j\nabla h_j=0$，$\mu_i\ge0$，$\mu_i g_i=0$。

求解器把它转化为序列子问题：**SLSQP**（序列二次规划，处理一般等式/不等式约束）、**trust-constr**（信赖域内点，适合较大规模 / 稀疏）、**L-BFGS-B**（仅边界约束）。全局性由**多起点**或 **differential_evolution** 提供。

## 求解工具

- `scipy.optimize.minimize(f, x0, method="SLSQP", bounds=..., constraints=...)` — 一般约束首选
- `method="trust-constr"` — 大规模 / 稀疏 / 更稳的内点法
- `method="L-BFGS-B"` — 仅边界约束的大规模光滑问题
- `scipy.optimize.differential_evolution` — 无导数**全局**优化（多峰 / 粗糙），可做多起点的替代
- `scipy.optimize.NonlinearConstraint` / `LinearConstraint` — 约束封装
- MATLAB 对照：`fmincon`（SQP/interior-point）、`ga`/`particleswarm`（全局）

## 代码模板

```python
"""
nonlinear_programming_template.py — SLSQP 约束优化 + 多起点求全局
用法: python nonlinear_programming_template.py   # 或 solver_submit.sh --type python
问题适配点: 改 objective / constraints / bounds。
"""
import json
import os
import numpy as np
from scipy.optimize import minimize, differential_evolution

np.random.seed(42)

# ---- 目标与约束（TODO: 替换为本题）----
def objective(x):
    # 示例：最小化 (x0-1)^2 + (x1-2.5)^2
    return (x[0] - 1.0) ** 2 + (x[1] - 2.5) ** 2

# 不等式约束写成 g(x) >= 0（scipy 'ineq' 约定）
constraints = [
    {"type": "ineq", "fun": lambda x:  x[0] - 2 * x[1] + 2},   # x0 - 2 x1 + 2 >= 0
    {"type": "ineq", "fun": lambda x: -x[0] - 2 * x[1] + 6},   # -x0 - 2 x1 + 6 >= 0
    {"type": "ineq", "fun": lambda x: -x[0] + 2 * x[1] + 2},   # -x0 + 2 x1 + 2 >= 0
]
bounds = [(0, None), (0, None)]

# ---- 1. 单点 SLSQP ----
res = minimize(objective, x0=[2.0, 0.0], method="SLSQP",
               bounds=bounds, constraints=constraints,
               options={"ftol": 1e-10, "maxiter": 500})
print(f"[SLSQP] x*={res.x}, f*={res.fun:.8f}, success={res.success}")

# ---- 2. 多起点（缓解局部最优）----
best = res
for _ in range(20):
    x0 = np.array([np.random.uniform(0, 5), np.random.uniform(0, 5)])
    r = minimize(objective, x0, method="SLSQP", bounds=bounds, constraints=constraints)
    if r.success and r.fun < best.fun:
        best = r
print(f"[multi-start] x*={best.x}, f*={best.fun:.8f}")

# ---- 3. 全局兜底（无导数）----
#    differential_evolution 只直接吃 bounds；约束用罚函数并入目标
def penalized(x):
    pen = sum(max(0.0, -c["fun"](x)) ** 2 for c in constraints)
    return objective(x) + 1e6 * pen
de = differential_evolution(penalized, bounds=[(0, 5), (0, 5)], seed=42, tol=1e-10)
print(f"[DE] x*={de.x}, f*={objective(de.x):.8f}")

os.makedirs("results", exist_ok=True)
with open("results/nlp_demo.json", "w") as fh:
    json.dump({"slsqp": {"x": res.x.tolist(), "f": float(res.fun)},
               "multistart": {"x": best.x.tolist(), "f": float(best.fun)},
               "de": {"x": de.x.tolist(), "f": float(objective(de.x))}},
              fh, ensure_ascii=False, indent=2)
print("written results/nlp_demo.json")
```

## 常见陷阱

1. **只跑一次就当全局最优**：SLSQP/trust-constr 是局部法；务必**多起点**或先 `differential_evolution` 粗找再精修，并报告是否多起点一致。
2. **约束符号约定错**：scipy `'ineq'` 要求 `fun(x) >= 0`；写成 $\le$ 形式会把可行 / 不可行搞反。
3. **量纲 / 尺度差异大**：变量或约束量级差几个数量级时优化器病态；先无量纲化 / 缩放，或对变量取 log。
4. **不可行初值**：SLSQP 从严重不可行点出发可能卡住；给可行或接近可行的 $x_0$。
5. **目标 / 约束不可微**：含 `abs`、`max`、分支的函数梯度不连续，有限差分梯度噪声大；改光滑化、或用无导数全局法。
6. **罚因子选取**：用罚函数并入全局法时，罚因子太小约束被违反、太大病态；分级加大或用增广拉格朗日。
7. **把 NLP 硬塞成 MILP**：含三角 / 相切 / 距离的几何优化线性化后误差大；可用 MILP 做**粗筛候选**，但最终解必须 NLP 精修（见 [[milp]]）。
8. **收敛容差太松**：`ftol/maxiter` 默认可能过早停；建模题收紧 `ftol=1e-10` 并检查 `res.success` 与 KKT 残差。
9. **梯度数值噪声**：默认有限差分步长对刚性目标不稳；可提供解析梯度 `jac=` 或用 trust-constr。
10. **缺对照与灵敏度**：NLP 结果应与基准 / 替代方案对比，并对关键参数做 ±10/20% 灵敏度（评委看重）。

## 在建模比赛中的典型应用

- **CUMCM 2024A 板凳龙**：问题 4 在"与螺线、圆弧相切且位于调头空间圆盘内"约束下**最小化调头曲线长度**（两段圆弧半径 / 切点为决策变量）；问题 5 求限速下最大龙头速率（可由速度放大系数解析得，亦可作约束优化，见 [[path_kinematics]]）。
- 2023A 定日镜场：镜面布局连续参数优化（与全局法配合）。
- 各类参数标定 / 最小二乘 / 带物理约束的连续设计优化。

## 参考文献

- Nocedal, J. & Wright, S. J. (2006). *Numerical Optimization* (2nd ed.). Springer.
- Boyd, S. & Vandenberghe, L. (2004). *Convex Optimization*. Cambridge.（KKT / 凸性判别）
- SciPy: `scipy.optimize.minimize` (SLSQP/trust-constr), `differential_evolution` 文档. https://docs.scipy.org/doc/scipy/
- 司守奎 & 孙兆亮 (2015). 《数学建模算法与应用》(第二版). 非线性规划章节.
