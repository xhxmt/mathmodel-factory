# 方程求根与事件定位 (Root Finding)

## 适用场景

- 求解 $f(x)=0$（单变量）或 $\mathbf F(\mathbf x)=\mathbf 0$（方程组）
- **定弦长 / 定弧长反解**：曲线上下一点的参数（铰接链递推，见 [[archimedean_spiral]]）
- **事件 / 临界时刻定位**：首次碰撞时刻、盘入终止时刻、阈值穿越时刻
- **一维可行性搜索**：求满足约束的临界参数（如最小可行螺距、最大可行速度）
- 相切条件求解（曲线与圆弧 $G^1$ 相切的切点 / 半径）

不适用：
- 带目标函数的最优化（求 min/max，不是求零点）→ `optimization/nonlinear_programming.md`
- 高维全局搜索 / 组合优化 → `metaheuristic/*`
- 线性方程组 → `numpy.linalg.solve`

## 核心假设

1. 目标方程 $f$ 在关心区间内连续（区间法要求 $f(lo)\,f(hi)<0$ 跨零）
2. 临界量随某参数**单调**变化（可行性搜索 / 二分的前提）
3. 方程组求解有合理初值（牛顿类局部方法对初值敏感）
4. 根存在且（在所取区间内）唯一，或已用方向 / 区间隔离出目标根

## 数学形式

**单变量**：找 $x^*$ 使 $f(x^*)=0$。
- 区间法（**Brent**）：需 $f(a)f(b)<0$，结合二分 + 反插值，无导数、必收敛、稳健 —— 竞赛首选。
- 牛顿法：$x_{k+1}=x_k-f(x_k)/f'(x_k)$，需导数、收敛快但可能发散。

**方程组**：找 $\mathbf x^*$ 使 $\mathbf F(\mathbf x^*)=\mathbf 0$，用 `fsolve`/`root`（拟牛顿，需初值）。

**可行性 / 阈值搜索**：定义可行性指标 $h(p)$（如最小碰撞裕度），临界参数 $p^*$ 满足 $h(p^*)=0$；若 $h$ 单调，二分 / Brent 即得临界值。

**事件定位**：随时间演化的量 $g(t)$ 首次满足 $g(t^*)=0$（如最近距离=0 的首次碰撞），在 $g$ 变号区间用 Brent 精化；ODE 过程用 `solve_ivp(events=...)`。

## 求解工具

- `scipy.optimize.brentq(f, a, b)` — 区间法求根，**稳健首选**（需跨零区间）
- `scipy.optimize.bisect` — 纯二分（更慢但更"傻瓜"稳）
- `scipy.optimize.newton(f, x0, fprime=...)` — 牛顿 / 割线（无 fprime 时割线法）
- `scipy.optimize.fsolve` / `scipy.optimize.root` — 非线性**方程组**
- `scipy.integrate.solve_ivp(..., events=...)` — ODE 积分中的事件定位

## 代码模板

```python
"""
root_finding_template.py — Brent 单变量求根 / fsolve 方程组 / 二分可行性搜索 / 事件定位
用法: python root_finding_template.py   # 或 solver_submit.sh --type python
"""
import json
import os
import numpy as np
from scipy.optimize import brentq, fsolve

np.random.seed(42)

# ---- 1. 单变量求根（区间法，稳健）----
f = lambda x: np.cos(x) - x
root = brentq(f, 0.0, 1.0, xtol=1e-12)       # f(0)=1>0, f(1)=cos1-1<0 跨零
print(f"[1] cos(x)=x 的根: x={root:.10f}, f(x)={f(root):.2e}")

# ---- 2. 非线性方程组（需初值）----
def system(v):
    x, y = v
    return [x**2 + y**2 - 1.0,     # 单位圆
            x - y]                  # 对角线
sol = fsolve(system, x0=[0.5, 0.5], full_output=False)
print(f"[2] 方程组解: {sol}, 残差={np.array(system(sol))}")

# ---- 3. 一维可行性搜索：求临界参数（h 单调跨零）----
#    例：某裕度 h(p) 随参数 p 增大由负变正，求临界 p*
def margin(p):
    return p - 1.7                  # TODO: 替换为真实可行性裕度(如最小碰撞裕度)
p_star = brentq(margin, 0.0, 5.0, xtol=1e-10)
print(f"[3] 临界参数 p* = {p_star:.6f}")

# ---- 4. 事件定位：g(t) 首次过零（变号区间内精化）----
g = lambda t: 2.0 - 0.5 * t        # TODO: 替换为真实事件函数(如最近距离-安全裕度)
ts = np.linspace(0, 10, 1001)
gv = g(ts)
idx = np.where(np.sign(gv[:-1]) != np.sign(gv[1:]))[0]
t_event = brentq(g, ts[idx[0]], ts[idx[0] + 1]) if len(idx) else None
print(f"[4] 首次事件时刻 t* = {t_event}")

os.makedirs("results", exist_ok=True)
with open("results/root_finding_demo.json", "w") as fh:
    json.dump({"root_cosx": root, "system_sol": list(map(float, sol)),
               "p_star": p_star, "t_event": t_event}, fh, ensure_ascii=False, indent=2)
print("written results/root_finding_demo.json")
```

## 常见陷阱

1. **brentq 不跨零**：`brentq` 要求 $f(a)f(b)<0$，否则报错；先粗扫描定出变号区间再调用。
2. **fsolve 初值不当跳到别的根 / 不收敛**：拟牛顿对初值敏感；用物理合理初值、或多初值尝试、或先 `brentq` 隔离。
3. **可行性指标非单调**：二分 / Brent 只对单调跨零的临界量有效；非单调时先网格扫描找区间再精化，否则漏掉真正临界点。
4. **粗网格扫描当成精确临界值**：网格只用来**定区间**，临界值必须用根查找精化（论文给临界值要给精化后的）。
5. **离散布尔可行性不可求根**：碰撞"是/否"是布尔、不可微；要构造**连续带符号**裕度（最近距离 / 穿透深度）作为 $g$，才能定位临界时刻。
6. **导数错误的牛顿法**：手写 `fprime` 出错会静默给错根；不确定就用 `brentq` / 割线法（无导数）。
7. **多根只取一个**：方程有多根时按物理区间分别隔离；别假设唯一。
8. **容差过松**：链式递推（如逐节反解）误差累积，`xtol` 设 $\le10^{-10}$ 并核对残差。
9. **事件函数在端点恰为零**：变号检测用 `sign` 比较时端点零值要单独处理，避免漏检 / 重复。
10. **ODE 事件不用 events 而靠采样**：连续过程的事件应用 `solve_ivp(events=...)` 精确捕获，采样网格会错过短暂事件。

## 在建模比赛中的典型应用

- **CUMCM 2024A 板凳龙**：定弦长反解每节把手（[[archimedean_spiral]]）；问题 2 用最近距离的根查找定位首次碰撞**终止时刻**（裕度来自 [[collision_detection]]）；问题 3 求**最小可行螺距**（可行性单调搜索）；问题 4 求圆弧与螺线的相切点 / 半径。
- 2019A 高压油管：单向阀开闭、压力达标时刻的事件定位（与 `dynamics/ode_system.md` 配合 `solve_ivp` events）。
- 各类"临界值 / 阈值 / 相切 / 首次达到"问题的精确求解。

## 参考文献

- Brent, R. P. (1973). *Algorithms for Minimization Without Derivatives*. Prentice-Hall.
- Press, W. H. et al. (2007). *Numerical Recipes* (3rd ed.), Ch. 9 (Root Finding). Cambridge.
- SciPy: `scipy.optimize.brentq` / `fsolve` / `root`, `scipy.integrate.solve_ivp` 文档. https://docs.scipy.org/doc/scipy/
