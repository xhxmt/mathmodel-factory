# 路径运动学与链式刚体速度传递 (Path Kinematics)

## 适用场景

- 多个点沿**同一条曲线**运动，相邻点由**刚性杆 / 铰接**连接（链式刚体）
- 已知"龙头"（首节）速度，求链条上各节速度 —— **速度沿杆传递**
- 速度放大 / 缩小系数分析：求全链最大速度、或反求使最大速度不超限的首节速度
- 沿参数曲线的运动：速度方向 = 曲线切向，大小由约束决定

不适用：
- 单点沿曲线运动、无链式约束（直接对弧长求导即可）
- 受力 / 动力学（加速度、惯性、力）→ 这是运动学(kinematics)，力学用 `dynamics/ode_system.md`
- 曲线几何本身（位置 / 弧长 / 切向量）→ `geometry/archimedean_spiral.md`

## 核心假设

1. 每个把手点严格沿已知曲线运动，瞬时速度方向 = 该点曲线**切向** $\hat{\mathbf t}_i$
2. 相邻点 $i,i+1$ 之间为**刚性**连接，杆长 $L$ 恒定
3. 刚性 ⇒ 两端速度在**杆方向**上的投影相等（杆不伸缩）
4. 运动光滑，曲线处处可导（切向量良定义）

## 数学形式

设相邻把手 $i,i+1$ 位置 $\mathbf P_i,\mathbf P_{i+1}$，杆方向单位向量

$$\hat{\mathbf u}=\frac{\mathbf P_{i+1}-\mathbf P_i}{\|\mathbf P_{i+1}-\mathbf P_i\|}.$$

刚性约束（杆长不变 ⇒ 沿杆相对速度为零）：

$$\mathbf v_i\cdot\hat{\mathbf u}=\mathbf v_{i+1}\cdot\hat{\mathbf u}.$$

每点速度沿曲线切向：$\mathbf v_i=v_i\,\hat{\mathbf t}_i$（$v_i$ 为速率标量）。代入得**速度传递递推**：

$$v_{i+1}=v_i\,\frac{\hat{\mathbf t}_i\cdot\hat{\mathbf u}}{\hat{\mathbf t}_{i+1}\cdot\hat{\mathbf u}}.$$

给定龙头速率 $v_0$ 即可逐节传递得到全链 $\{v_i\}$。

**速度放大系数**：$A=\max_i v_i / v_0$。反问题（限速 $v_{\max}$ 下求最大龙头速率）：$v_0^{\*}=v_{\max}/A$（$A$ 与 $v_0$ 无关，几何决定）。

## 求解工具

- `numpy` — 切向量、点积、递推
- `geometry/archimedean_spiral.md` 的 `spiral_point` + 切向量（解析或差分）
- `scipy.optimize.brentq` — 反问题中若 $A$ 随构型变化需在时间上扫描求极值（见 [[root_finding]]）
- 可视化：各把手速率随节序 / 随时间曲线

## 代码模板

```python
"""
path_kinematics_template.py — 链式刚体沿螺线的速度传递（板凳龙式）
给定龙头速率，递推各把手速率，并算速度放大系数。
用法: python path_kinematics_template.py   # 或 solver_submit.sh --type python
"""
import json
import os
import numpy as np
from scipy.optimize import brentq

np.random.seed(42)

# ---- 螺线几何（与 archimedean_spiral 一致）----
def spiral_point(theta, a, b):
    r = a + b * theta
    return np.array([r * np.cos(theta), r * np.sin(theta)])

def spiral_tangent(theta, a, b):
    """解析切向量 dr/dtheta，归一化。"""
    r = a + b * theta
    dx = b * np.cos(theta) - r * np.sin(theta)
    dy = b * np.sin(theta) + r * np.cos(theta)
    t = np.array([dx, dy])
    return t / np.linalg.norm(t)

def next_handle_theta(theta_i, L, a, b, step=0.01, max_span=6 * np.pi):
    """最近弦长反解：螺线上弦长非单调，扫描第一次跨越 L 的小区间再 brentq。"""
    Pi = spiral_point(theta_i, a, b)
    g = lambda th: np.linalg.norm(spiral_point(th, a, b) - Pi) - L
    lo = theta_i
    th = theta_i + step
    while th <= theta_i + max_span:
        if g(lo) < 0.0 <= g(th):
            return brentq(g, lo, th, xtol=1e-12)
        lo = th
        th += step
    raise ValueError("未找到弦长解")

# ---- 速度传递 ----
def propagate_speeds(thetas, v_head, a, b):
    """给定各把手 theta 与龙头速率，返回各把手速率。"""
    pts = np.array([spiral_point(t, a, b) for t in thetas])
    tang = np.array([spiral_tangent(t, a, b) for t in thetas])
    v = [v_head]
    for i in range(len(thetas) - 1):
        u = pts[i + 1] - pts[i]
        u = u / np.linalg.norm(u)
        proj_i = abs(tang[i] @ u)
        proj_j = abs(tang[i + 1] @ u)
        v.append(v[-1] * proj_i / max(proj_j, 1e-12))
    return np.array(v)

# ---- 演示 ----
pitch = 0.55
b = pitch / (2 * np.pi)
a = 0.0
L_head, L_body = 2.86, 1.65
n_body = 6
v_head = 1.0                                   # 龙头速率 1 m/s

theta = [16 * 2 * np.pi]
for L in [L_head] + [L_body] * n_body:
    theta.append(next_handle_theta(theta[-1], L, a, b))

speeds = propagate_speeds(theta, v_head, a, b)
amp = speeds.max() / v_head
print("各把手速率 (m/s):")
for k, s in enumerate(speeds):
    print(f"  handle {k:2d}: v={s:.6f}")
print(f"速度放大系数 A = max v / v_head = {amp:.6f}")
# 反问题：限速 2 m/s 下的最大龙头速率
print(f"限速 2 m/s 下最大龙头速率 v0* = {2.0 / amp:.6f} m/s")

os.makedirs("results", exist_ok=True)
with open("results/path_kinematics_demo.json", "w") as f:
    json.dump({"v_head": v_head, "speeds": speeds.tolist(),
               "amplification": float(amp)}, f, ensure_ascii=False, indent=2)
print("written results/path_kinematics_demo.json")
```

## 常见陷阱

1. **以为所有节速率相同**：刚性链上速率沿杆传递，因切向与杆向夹角不同而**逐节变化**；龙头 1 m/s 不代表龙尾 1 m/s。
2. **投影分母趋零**：当某节切向几乎垂直于杆（$\hat{\mathbf t}_{i+1}\cdot\hat{\mathbf u}\approx0$）时速率发散，是真实的运动学奇异（尖点 / 急转），需在论文中讨论并设保护下限。
3. **切向量符号 / 方向**：传递公式对切向取向敏感；统一沿运动方向，或对投影取绝对值（模板用 `abs`）。
4. **混淆速率与速度向量**：传递的是沿切向的**速率标量**；位置、方向来自几何，别把矢量分量直接当速率。
5. **速度放大系数当成依赖 $v_0$**：$A$ 是纯几何量（与龙头速率成正比关系中约掉），反求限速龙头速率时可直接 $v_0^*=v_{\max}/A$。
6. **只看某一时刻**：最大速度放大常出现在特定构型 / 时刻；反问题要在运动过程中扫描求 $A$ 的极大值（配合根查找 / 网格 + 精化）。
7. **切向量用粗差分**：差分步长不当会污染投影比；优先解析切向量（模板给出），必要时用中心差分并收紧步长。
8. **忽略与碰撞约束的耦合**：速度分析的构型必须是几何可行（无碰撞）的；与 [[collision_detection]] 联合判定。
9. **单位 / 量纲**：杆长 m、速率 m/s；角度弧度。混用导致放大系数错。
10. **缺速率分布图**：运动学题应给出速率沿节序 / 随时间的曲线，仅给极值扣分。

## 在建模比赛中的典型应用

- **CUMCM 2024A 板凳龙**：问题 1 求各把手速度（本文递推，几何来自 [[archimedean_spiral]]）；问题 5 求"各把手速度不超过 2 m/s 时龙头最大行进速度" = 用速度放大系数反求 $v_0^*$。
- 链传动 / 履带 / 索道 / 传送链上点速度传递。
- 机器人串联连杆的末端速度（雅可比的几何特例）。

## 参考文献

- 司守奎 & 孙兆亮 (2015). 《数学建模算法与应用》(第二版). 运动学 / 几何建模章节.
- Spong, M. W., Hutchinson, S., & Vidyasagar, M. (2006). *Robot Modeling and Control*. Wiley.（连杆速度 / 雅可比）
- do Carmo, M. P. (1976). *Differential Geometry of Curves and Surfaces*. Prentice-Hall.（切向量）
