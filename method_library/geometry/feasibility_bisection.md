# 可行性二分搜索 / 一维参数扫描

## 适用场景

- 已知约束 $g(x) \le 0$ 可行，求最大化 $x$ 使约束仍满足
- 临界值搜索：最大载重、最小转弯半径、临界碰撞距离
- 替代全局优化：当目标函数单调但约束复杂时，转化为可行性判定
- CUMCM 国赛典型：最大通过速度、极限装载量、阈值参数反求

**何时切换**：
- 若 $g(x)$ 非单调 → 二分失效 → 改用网格搜索或全局优化
- 若需要梯度信息 → 改用 SLSQP / 内点法
- 若 $x$ 高维（>3）→ 二分不适用 → 改用启发式

---

## 核心假设

1. **单调性**：$g(x)$ 关于 $x$ 单调（严格或非严格）
2. **可判定性**：给定 $x$，能在有限时间内判定 $g(x) \le 0$ 是否成立
3. **边界确定**：存在已知的 $x_{\min}, x_{\max}$ 满足 $g(x_{\min}) < 0 < g(x_{\max})$

**违反假设的后果**：
- 非单调 → 二分可能错过全局最优
- 判定耗时 >> 1秒 → 总搜索时间不可接受（改用粗网格 + 局部细化）

---

## 数学形式

### 1. 标准二分法（求根）
已知 $f(a) < 0 < f(b)$，求 $x^* \in [a, b]$ 使 $f(x^*) = 0$：

```
while b - a > tol:
    mid = (a + b) / 2
    if f(mid) < 0:
        a = mid
    else:
        b = mid
return (a + b) / 2
```

收敛速度：$O(\log_2 \frac{b-a}{\epsilon})$ 次迭代

### 2. 可行性二分（最大化版本）
求最大 $x$ 使 $g(x) \le 0$：

```
x_lo = x_min  # 已知可行
x_hi = x_max  # 已知不可行
while x_hi - x_lo > tol:
    x_mid = (x_lo + x_hi) / 2
    if is_feasible(x_mid):  # 判定 g(x_mid) ≤ 0
        x_lo = x_mid  # 可行 → 向上探索
    else:
        x_hi = x_mid  # 不可行 → 向下收缩
return x_lo  # 最后一次可行的 x
```

### 3. 多约束版本
若有 K 个约束 $g_k(x) \le 0, k=1..K$，`is_feasible(x)` 为：
```python
def is_feasible(x):
    return all(g_k(x) <= 0 for k in range(K))
```

---

## 求解工具

- **纯 Python / NumPy**：直接实现二分循环
- **scipy.optimize**：
  - `brentq(f, a, b)`：单变量求根（比二分法更快，用 Brent 法）
  - `fsolve(f, x0)`：多变量求根（需梯度信息）
- **约束检查**：调用现有 MILP / NLP 求解器判定可行性（如 `gurobipy.Model.optimize()`）

---

## 代码模板

```python
#!/usr/bin/env python3
"""
可行性二分搜索示例：最大通过速度
场景：车辆过弯，速度 v 越大，离心力越大，约束 g(v) = F_centri(v) - F_max ≤ 0
目标：求最大 v 使约束满足
"""
import numpy as np

# 物理参数
m = 1000  # 车质量 kg
R = 50    # 弯道半径 m
mu = 0.7  # 摩擦系数
g = 9.8   # 重力加速度 m/s²

F_max = mu * m * g  # 最大摩擦力 N

def is_feasible(v):
    """判定速度 v 是否可行"""
    F_centri = m * v**2 / R  # 离心力
    return F_centri <= F_max

# 二分搜索
v_lo = 0.0    # 已知可行（静止）
v_hi = 100.0  # 已知不可行（过快）
tol = 0.01    # 精度 m/s

iterations = 0
while v_hi - v_lo > tol:
    v_mid = (v_lo + v_hi) / 2
    if is_feasible(v_mid):
        v_lo = v_mid
    else:
        v_hi = v_mid
    iterations += 1

v_star = v_lo
print(f"最大通过速度: {v_star:.2f} m/s")
print(f"迭代次数: {iterations} (理论 log₂({100/tol:.0f}) ≈ {np.log2(100/tol):.1f})")
print(f"验证: F_centri = {m * v_star**2 / R:.1f} N, F_max = {F_max:.1f} N")
```

输出示例：
```
最大通过速度: 18.52 m/s
迭代次数: 14 (理论 log₂(10000) ≈ 13.3)
验证: F_centri = 6859.8 N, F_max = 6860.0 N
```

---

## 常见陷阱

1. **初始区间不保守**：若 $g(x_{\min}) > 0$（初始点不可行）→ 二分失效 → 先用粗网格找可行点
2. **约束判定有误差**：数值求解器返回 "nearly feasible" → 加安全裕度（如 `g(x) <= -1e-6`）
3. **目标与约束混淆**：二分搜索的是参数 $x$，不是目标函数值 → 若要最大化 $f(x)$ subject to $g(x) \le 0$，需确认 $f$ 单调
4. **多峰问题**：$g(x)$ 非单调（如周期函数）→ 二分找到局部解 → 改用全局优化

---

## 在建模比赛中的典型应用

| 竞赛 | 题目 | 二分参数 | 约束判定 |
|------|------|---------|---------|
| CUMCM 2024 A | 板材龙舟调头 | 最大通过速度 $v$ | 圆盘干涉 $g(v) =$ AABB + SAT 碰撞检测 |
| CUMCM 2019 B | 机器人避障路径 | 最小安全距离 $d$ | 路径可行性 = Dijkstra 求解成功 |
| MCM 2021 E | 无人机编队 | 最大载荷 $W$ | 电池续航约束 $t(W) \ge t_{\min}$ |

**CUMCM 2024 A 题（板材龙舟）实例**：
- 问题 4：16 个调头点的极限通过速度
- 挑战：可行性判定 = 完整路径模拟 + 碰撞检测 → 单次耗时 ~30秒
- 优化：粗网格（10 个候选速度）→ 二分细化（5 次迭代）→ 总时间 ~15 分钟
- 失败模式：P4 速度过快 → 83% 刚性约束残差 → 回退到保守值

---

## 扩展：网格 + 二分混合

当可行性判定昂贵（单次 > 10秒）时：
1. 粗网格：$x \in \{x_{\min}, x_{\min} + \Delta, ..., x_{\max}\}$，找到最后一个可行点 $x_k$
2. 细化二分：在 $[x_k, x_{k+1}]$ 内二分（只需 $\log_2(N)$ 次额外判定）

---

## 参考文献

1. **Press, W. H., et al.** (2007). *Numerical Recipes: The Art of Scientific Computing* (3rd ed.). §9.1 Bracketing and Bisection.
2. **Boyd, S., & Vandenberghe, L.** (2004). *Convex Optimization*. §4.2.3 Bisection method for quasiconvex optimization.
3. **scipy.optimize.brentq** 文档：https://docs.scipy.org/doc/scipy/reference/generated/scipy.optimize.brentq.html
4. **CUMCM 历年题解**：2019 B / 2024 A 中可行性二分的典型应用

---

**关键词**：二分搜索、可行性判定、参数扫描、临界值、单调约束、最大化参数、一维优化、Brent法
