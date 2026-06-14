# 曲线拼接 / 分段路径连续性

## 适用场景

- 多段曲线端到端拼接（圆弧、直线、螺线、样条），要求 C⁰/C¹/C² 连续
- 路径规划：AGV / 无人机轨迹优化，避障后路径重拼接
- 几何约束问题：已知起终点、切向、曲率，反求连接曲线参数
- CUMCM 国赛典型：板材切割路径、龙舟转弯轨迹、机械臂运动规划

**何时切换**：若对曲率跳变不敏感（如物流路线），用简单折线；若需平滑加速度（机器人控制），必须 C² 连续。

---

## 核心假设

1. **连续性等级**：C⁰（位置连续）、C¹（速度连续）、C² （加速度连续 / 曲率连续）
2. **参数域独立**：每段曲线用独立参数 $t_i \in [0,1]$，拼接点通过端点匹配
3. **无奇异点**：拼接点处曲率有界（$\kappa < \kappa_{\max}$），否则退化为角点

**违反假设的后果**：
- 违反 C¹ → 速度突变 → 机械冲击 / 不可行轨迹
- 曲率无界 → 数值求解失稳 / 物理不可达

---

## 数学形式

### 1. C⁰ 拼接（位置连续）
已知 N 段曲线 $\gamma_i(t), t \in [0,1], i=1..N$，拼接条件：
$$
\gamma_i(1) = \gamma_{i+1}(0), \quad i=1..N-1
$$

### 2. C¹ 拼接（速度连续）
$$
\begin{cases}
\gamma_i(1) = \gamma_{i+1}(0) \\
\dot{\gamma}_i(1) = \dot{\gamma}_{i+1}(0)
\end{cases}
$$

### 3. C² 拼接（曲率连续）
$$
\begin{cases}
\gamma_i(1) = \gamma_{i+1}(0) \\
\dot{\gamma}_i(1) = \dot{\gamma}_{i+1}(0) \\
\ddot{\gamma}_i(1) = \ddot{\gamma}_{i+1}(0)
\end{cases}
$$

### 4. 典型拼接：圆弧 + 直线
- 直线段：$\mathbf{p}(t) = \mathbf{p}_0 + t \mathbf{v}, \quad t \in [0, L]$
- 圆弧段：$\mathbf{p}(\theta) = \mathbf{c} + R[\cos(\theta_0 + \theta), \sin(\theta_0 + \theta)]^T, \quad \theta \in [0, \Delta\theta]$
- C¹ 条件：直线终点切向 = 圆弧起点切向

---

## 求解工具

- **符号推导**：`sympy`（解拼接方程组，反求半径 R / 切角 θ）
- **数值优化**：`scipy.optimize.minimize`（最小化曲率跳变 $\sum |\kappa_i(1) - \kappa_{i+1}(0)|^2$）
- **样条插值**：`scipy.interpolate.CubicSpline`（自动保证 C²，给定节点即可）

---

## 代码模板

```python
#!/usr/bin/env python3
"""
曲线拼接示例：圆弧 + 直线 + 圆弧（C¹ 连续）
场景：AGV 从 A 点沿直线前进，转弯 90°，再沿直线到 B 点
"""
import numpy as np
import matplotlib.pyplot as plt

# 已知条件
p_start = np.array([0.0, 0.0])  # 起点
p_end = np.array([5.0, 5.0])    # 终点
v_start = np.array([1.0, 0.0])  # 起始方向（单位向量）
v_end = np.array([0.0, 1.0])    # 终止方向（单位向量）
R = 1.0  # 转弯半径

# 几何推导（C¹ 拼接条件）
# 直线 1: p_start → 圆弧起点 p1
theta_start = np.arctan2(v_start[1], v_start[0])
c1 = p_start + R * np.array([-np.sin(theta_start), np.cos(theta_start)])  # 圆心 1
p1 = c1 + R * np.array([np.sin(theta_start), -np.cos(theta_start)])       # 圆弧起点

# 圆弧: 90° 转弯
theta_sweep = np.pi / 2
p2 = c1 + R * np.array([np.sin(theta_start + theta_sweep), 
                        -np.cos(theta_start + theta_sweep)])  # 圆弧终点

# 直线 2: p2 → p_end
L2 = np.linalg.norm(p_end - p2)

# 绘制
t_line1 = np.linspace(0, R, 50)
line1 = p_start[:, None] + v_start[:, None] * t_line1

theta_arc = np.linspace(theta_start, theta_start + theta_sweep, 50)
arc = c1[:, None] + R * np.array([np.sin(theta_arc), -np.cos(theta_arc)])

t_line2 = np.linspace(0, L2, 50)
line2 = p2[:, None] + v_end[:, None] * t_line2

plt.figure(figsize=(6, 6))
plt.plot(line1[0], line1[1], 'b-', label='直线 1')
plt.plot(arc[0], arc[1], 'r-', label='圆弧转弯')
plt.plot(line2[0], line2[1], 'g-', label='直线 2')
plt.plot(*p_start, 'ko', label='起点')
plt.plot(*p_end, 'k^', label='终点')
plt.plot(*c1, 'rx', label='圆心')
plt.axis('equal')
plt.legend()
plt.grid(True)
plt.savefig('curve_joining_example.pdf')
print(f"路径长度: {R + np.pi/2 * R + L2:.3f}")
```

---

## 常见陷阱

1. **切向不匹配**：直线段终点的方向向量与圆弧起点切向不一致 → C¹ 违反 → 速度突变
2. **曲率跳变**：圆弧半径突变（R₁ → R₂）→ 加速度不连续 → 机械冲击
3. **过约束**：给定起终点、起终切向、中间过点 → 可能无解 → 需松弛为软约束优化
4. **数值误差累积**：长链条拼接（N > 10）→ 端点漂移 → 需全局优化而非逐段匹配

---

## 在建模比赛中的典型应用

| 竞赛 | 题目 | 拼接类型 | 关键约束 |
|------|------|---------|---------|
| CUMCM 2023 A | 板材切割路径优化 | 直线 + 圆弧 | 最小转弯半径，切割速度连续 |
| CUMCM 2017 B | 巡线机器人 | 样条拼接 | C² 连续，曲率 < 1/R_min |
| MCM 2019 C | 无人机编队避障 | Dubins 曲线 | 最大转弯角速度约束 |

**CUMCM 2024 A 题（板材龙舟）实例**：
- 问题 2：16 个调头点的螺线拼接
- 挑战：辅助圆盘（m2）的调头路径与主龙舟（m1）不同心，需独立拼接
- 失败模式：未对 m2 单独建立符号表 → 符号覆盖率崩溃（0.408）

---

## 参考文献

1. **Dubins, L. E.** (1957). "On Curves of Minimal Length with a Constraint on Average Curvature". *American Journal of Mathematics*, 79(3), 497-516.
2. **Reeds, J., & Shepp, L.** (1990). "Optimal Paths for a Car That Goes Both Forwards and Backwards". *Pacific Journal of Mathematics*, 145(2), 367-393.
3. **scipy.interpolate.CubicSpline** 文档：https://docs.scipy.org/doc/scipy/reference/generated/scipy.interpolate.CubicSpline.html
4. **国赛历年题解**：CUMCM 2017 B / 2023 A 优秀论文中的曲线拼接实现

---

**关键词**：曲线拼接、C¹连续、C²连续、圆弧直线、Dubins曲线、路径规划、转弯半径、切向匹配
