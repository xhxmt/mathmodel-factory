# 常微分方程组 (ODE Systems)

## 适用场景

- 连续时间动力学过程：传染病传播 / 化学反应 / 人口生态 / 力学振动 / 控制系统
- 状态变量满足 $\frac{d\mathbf{X}}{dt} = \mathbf{f}(\mathbf{X}, t, \boldsymbol{\theta})$，可显式写出右端
- 初值（或边值）问题
- 需要参数辨识（已知数据反推参数）—— 配合最小二乘 / MCMC

不适用：
- 空间分布关键 → PDE（偏微分方程）
- 离散事件主导 → 离散事件仿真 / Agent-based
- 含强随机扰动 → SDE（随机微分方程）
- 系统刚性极强且需高精度 → 专用 implicit Runge-Kutta（不是 RK45）

## 核心假设

1. 系统状态可由有限维向量 $\mathbf{X} \in \mathbb{R}^n$ 完整描述
2. 演化光滑（$\mathbf{f}$ Lipschitz 连续即可保证唯一解）
3. 参数 $\boldsymbol{\theta}$ 在感兴趣时间窗内常数（变参需写 $\boldsymbol{\theta}(t)$）
4. 时间是关键自变量；其他变量被吸收进 $\mathbf{X}$ 或 $\boldsymbol{\theta}$

## 经典例

- **SIR 传染病**：$\dot S = -\beta S I, \dot I = \beta S I - \gamma I, \dot R = \gamma I$
- **Lotka-Volterra 捕食**：$\dot x = \alpha x - \beta x y, \dot y = \delta x y - \gamma y$
- **van der Pol 振子**：$\ddot x - \mu(1-x^2)\dot x + x = 0$
- **Lorenz 系统**（混沌）：$\dot x = \sigma(y-x), \dot y = x(\rho - z) - y, \dot z = xy - \beta z$
- **化学反应链**（刚性）：$\dot A = -k_1 A, \dot B = k_1 A - k_2 B, \dot C = k_2 B$，当 $k_1 \gg k_2$ 时刚性

## 求解工具

- `scipy.integrate.solve_ivp(fun, t_span, y0, method, args)` — 现代统一接口
  - `method='RK45'`：默认，4(5) 阶 Runge-Kutta，非刚性
  - `method='LSODA'`：自适应在刚性/非刚性间切换（首选稳健选择）
  - `method='Radau'`：刚性问题专用
- `scipy.integrate.odeint`（旧接口，仍可用）
- 大规模 / GPU：`diffrax`（JAX）、`torchdiffeq`（PyTorch）
- 参数辨识：`scipy.optimize.least_squares` 或 `lmfit`

## 代码模板

```python
"""
ode_template.py — SIR 模型示例 + 参数辨识 + 灵敏度
用法: python ode_template.py
"""
import numpy as np
from scipy.integrate import solve_ivp
from scipy.optimize import least_squares

np.random.seed(42)

# ---- 1. 定义 ODE 右端 ----
def sir_rhs(t, y, beta, gamma):
    S, I, R = y
    N = S + I + R
    dS = -beta * S * I / N
    dI = beta * S * I / N - gamma * I
    dR = gamma * I
    return [dS, dI, dR]

# ---- 2. 正向求解 ----
N = 1_000_000
I0, R0 = 100, 0
S0 = N - I0 - R0
y0 = [S0, I0, R0]
t_span = (0, 180)
t_eval = np.linspace(*t_span, 181)

true_params = (0.3, 0.1)  # beta, gamma
sol = solve_ivp(sir_rhs, t_span, y0, args=true_params,
                t_eval=t_eval, method="LSODA",
                rtol=1e-8, atol=1e-10)
S, I, R = sol.y

# 模拟观测: 真实 I 加正态噪声
I_obs = I + np.random.normal(0, 200, size=len(t_eval))

# ---- 3. 参数辨识 (反问题) ----
def residuals(params, t_eval, y0, I_obs):
    sol = solve_ivp(sir_rhs, (t_eval[0], t_eval[-1]), y0,
                    args=tuple(params), t_eval=t_eval,
                    method="LSODA", rtol=1e-8)
    if not sol.success:
        return np.full_like(I_obs, 1e6)
    return sol.y[1] - I_obs

x0 = [0.5, 0.2]  # 初始猜测
result = least_squares(residuals, x0, args=(t_eval, y0, I_obs),
                       bounds=([0.01, 0.01], [2.0, 1.0]))
beta_hat, gamma_hat = result.x
print(f"真实参数: beta={true_params[0]}, gamma={true_params[1]}")
print(f"估计参数: beta={beta_hat:.4f}, gamma={gamma_hat:.4f}")
print(f"R0 估计 = {beta_hat/gamma_hat:.3f}")

# ---- 4. 灵敏度分析 (有限差分) ----
def peak_infected(params):
    sol = solve_ivp(sir_rhs, t_span, y0, args=tuple(params),
                    t_eval=t_eval, method="LSODA", rtol=1e-8)
    return sol.y[1].max()

eps = 1e-3
base = peak_infected([beta_hat, gamma_hat])
dPdbeta  = (peak_infected([beta_hat + eps, gamma_hat]) - base) / eps
dPdgamma = (peak_infected([beta_hat, gamma_hat + eps]) - base) / eps
print(f"峰值感染对 beta 灵敏度: {dPdbeta:.0f}")
print(f"峰值感染对 gamma 灵敏度: {dPdgamma:.0f}")
```

## 常见陷阱

1. **刚性方程用 RK45**：会跑非常慢甚至发散。化学反应、电路、酶动力学十有八九是刚性 → 用 `LSODA` 或 `Radau`。
2. **默认精度不够**：`solve_ivp` 默认 `rtol=1e-3, atol=1e-6`，长时间积分误差累积可达 1%。建模题应**显式**收紧到 `rtol=1e-8, atol=1e-10` 并在论文中报告。
3. **守恒量漂移**：质量守恒 / 能量守恒系统积分一段时间后总量漂移，是步长不够或 method 不当的信号。可加约束 + DAE 求解。
4. **参数辨识陷入局部极小**：least_squares 是局部方法。多组初值 + 取最优；或先用全局优化（differential_evolution）粗调。
5. **量纲不齐**：参数差几个数量级时优化器不稳定。先无量纲化或对参数 log 变换。
6. **观测数据稀疏**：与连续 ODE 对接时插值/积分误差大。考虑用合适的目标函数（带噪声模型）+ 不要在没有观测点处人工插值。
7. **初值未知**：把 $\mathbf{X}_0$ 一并放进参数向量优化（注意维度膨胀）。
8. **数值奇异**：右端含 1/x 或 log(x) 类奇异点 → 加保护项 `max(x, eps)`。
9. **多解性 / 不可辨识**：SIR 中只观测 I 时 $\beta$ 和 $\gamma$ 可能存在缩放共线，需要先做参数辨识性分析（Fisher 信息矩阵）。
10. **可视化遗漏相图**：动力学论文必须有相图（phase plot）；仅画时间序列扣分。

## 在建模比赛中的典型应用

- 传染病传播类（SIR、SEIR 及其变种）
- 化学反应器最优化（设计反应温度 / 浓度曲线）
- 力学题中弹道、振动、稳定性分析
- 资源 / 生态 / 人口动力学
- 含控制变量的最优控制（与 Pontryagin 极大值原理结合）
- 真实 CUMCM 案例：CUMCM 历年含"动力学"或"过程演化"的题目多以 ODE 为骨架（如 2016 A 题系泊系统、2020 A 题炉温优化）

## 参考文献

- Hairer, E., Nørsett, S. P., & Wanner, G. (1993). *Solving Ordinary Differential Equations I: Nonstiff Problems* (2nd ed.). Springer.
- Hairer, E., & Wanner, G. (1996). *Solving Ordinary Differential Equations II: Stiff and Differential-Algebraic Problems* (2nd ed.). Springer.
- SciPy documentation: https://docs.scipy.org/doc/scipy/reference/generated/scipy.integrate.solve_ivp.html
- 司守奎 & 孙兆亮 (2015). 《数学建模算法与应用》(第二版). 第 11–12 章.
