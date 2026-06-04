# 阿基米德螺线与曲线运动学 (Archimedean Spiral)

## 适用场景

- 物体沿**等距螺线**运动：相邻两圈径向间距恒定（螺距 $p$ 固定）
- 已知曲线参数化，需要：位置、弧长、切/法向量、曲率
- **定弧长 / 定弦长反解**：已知曲线上一点，求沿曲线走过固定弧长、或与该点直线距离为固定值的另一点（铰接链 / 跟随运动的核心递推）
- 盘入 / 盘出螺线、螺旋线圈布局、等距扫描轨迹

不适用：
- 轨迹由微分方程隐式给出（状态随时间演化）→ `dynamics/ode_system.md`
- 只关心刚体之间是否碰撞，不关心曲线 → `geometry/collision_detection.md`
- 沿曲线的**速度传递**（链式刚体）→ `dynamics/path_kinematics.md`（本文给几何，那里给运动学）

## 核心假设

1. 曲线可显式参数化为 $\mathbf r(\theta)=(x(\theta),y(\theta))$，且 $\theta$ 在工作区间内单调
2. 等距螺线径向增长率 $b$ 为常数：$r=a+b\theta$，螺距 $p=2\pi b$
3. 运动点严格落在曲线上（把手中心在螺线上），不脱离
4. 弧长积分用数值积分即可达到建模所需精度（无需闭式）

## 数学形式

**等距（阿基米德）螺线**，极坐标 $r=a+b\theta$，直角坐标参数化：

$$x(\theta)=(a+b\theta)\cos\theta,\qquad y(\theta)=(a+b\theta)\sin\theta$$

相邻两圈径向间距（螺距）$p=2\pi b$，故由螺距定 $b=p/(2\pi)$。

**弧长**（无初等表达式，数值积分）：

$$s(\theta)=\int_{0}^{\theta}\sqrt{r^2+(dr/d\theta)^2}\,d t=\int_{0}^{\theta}\sqrt{(a+bt)^2+b^2}\,dt$$

**切向量 / 单位法向量 / 曲率**（一般参数曲线）：

$$\mathbf r'(\theta)=(x',y'),\quad \hat{\mathbf n}=\frac{(-y',x')}{|\mathbf r'|},\quad \kappa=\frac{|x'y''-y'x''|}{(x'^2+y'^2)^{3/2}},\quad R=1/\kappa$$

**定弦长反解（铰接递推核心）**：已知曲线上一点对应参数 $\theta_i$，求下一点参数 $\theta_{i+1}$ 使两点**直线距离**等于杆长 $L$：

$$g(\theta)=\|\mathbf r(\theta)-\mathbf r(\theta_i)\|_2-L=0$$

沿盘入方向在 $(\theta_i,\theta_i+\Delta_{\max})$ 内对 $g$ 用 `brentq` 求根（$g$ 在该方向单调跨零，见 `numerical/root_finding.md`）。

## 求解工具

- `scipy.integrate.quad(f, 0, theta)` — 弧长数值积分（高精度，返回误差估计）
- `scipy.optimize.brentq(g, lo, hi)` — 定弦长 / 定弧长反解（区间法，稳健，需 $g(lo)g(hi)<0$）
- `numpy` — 向量化坐标 / 切法向量
- 可视化：`matplotlib`（盘入轨迹 + 各把手点；论文需配轨迹图）

## 代码模板

```python
"""
archimedean_spiral_template.py — 等距螺线位置 / 弧长 / 定弦长反解（板凳龙式铰接递推）
用法: python archimedean_spiral_template.py    # 或 solver_submit.sh --type python
固定随机种子；结果写 results/spiral_demo.json，并打印关键量。
"""
import json
import os
import numpy as np
from scipy.integrate import quad
from scipy.optimize import brentq

np.random.seed(42)

# ---- 1. 螺线参数化（盘入：theta 减小则向心；这里用 r=a+b*theta, b>0）----
def spiral_point(theta, a, b):
    r = a + b * theta
    return np.array([r * np.cos(theta), r * np.sin(theta)])

def spiral_speed_integrand(t, a, b):
    return np.hypot(a + b * t, b)          # |r'(t)| = sqrt((a+bt)^2 + b^2)

def arc_length(theta, a, b, theta0=0.0):
    val, _ = quad(spiral_speed_integrand, theta0, theta, args=(a, b))
    return val

# ---- 2. 定弦长反解：下一铰接点（直线距离 = L）----
def next_handle_theta(theta_i, L, a, b, step=0.01, max_span=6 * np.pi):
    """沿 theta 增大方向找**最近**的 theta_{i+1}，使 |P(theta)-P(theta_i)| = L。
    螺线上弦长随 theta 非单调（绕圈会反复跨越 L），所以不能用大区间直接
    brentq；先按小步长扫描定位第一次由 <L 跨到 >=L 的区间，再精化。"""
    Pi = spiral_point(theta_i, a, b)
    g = lambda th: np.linalg.norm(spiral_point(th, a, b) - Pi) - L
    lo = theta_i
    th = theta_i + step
    while th <= theta_i + max_span:
        if g(lo) < 0.0 <= g(th):              # 第一次跨越目标弦长
            return brentq(g, lo, th, xtol=1e-12)
        lo = th
        th += step
    raise ValueError("搜索跨度内未找到弦长解，增大 max_span 或检查参数")

# ---- 3. 演示：板凳龙式链条（龙头 + 若干龙身），螺距 0.55 m ----
pitch = 0.55
b = pitch / (2 * np.pi)
a = 0.0
L_head, L_body = 2.86, 1.65            # 前后孔中心距（m）
n_body = 5

theta = [16 * 2 * np.pi]               # 龙头前把手起始（第16圈附近）
chords = [L_head] + [L_body] * n_body
for L in chords:
    theta.append(next_handle_theta(theta[-1], L, a, b))

pts = np.array([spiral_point(t, a, b) for t in theta])
seglen = np.linalg.norm(np.diff(pts, axis=0), axis=1)

print("各把手坐标 (m):")
for k, (t, P) in enumerate(zip(theta, pts)):
    print(f"  handle {k:2d}: theta={t:8.4f}  (x,y)=({P[0]:8.4f},{P[1]:8.4f})")
print("实测相邻直线距离 vs 目标杆长:")
for k, (d, L) in enumerate(zip(seglen, chords)):
    print(f"  seg {k}: {d:.6f} vs {L:.6f}  err={abs(d-L):.2e}")
print(f"龙头处弧长 s = {arc_length(theta[0], a, b):.4f} m")

os.makedirs("results", exist_ok=True)
with open("results/spiral_demo.json", "w") as f:
    json.dump({"pitch": pitch, "theta": theta,
               "points": pts.tolist(), "seg_err_max": float(np.max(np.abs(seglen - chords)))},
              f, ensure_ascii=False, indent=2)
print("written results/spiral_demo.json")
```

## 常见陷阱

1. **混淆螺距与 $b$**：等距螺线相邻圈径向间距是 $2\pi b$，不是 $b$。由螺距定 $b=p/(2\pi)$。
2. **弧长当成 $r\theta$**：螺线弧长**没有**初等公式，必须数值积分 $\int\sqrt{r^2+r'^2}\,d\theta$；用 $r\theta$ 近似会系统性偏小。
3. **反解用 fsolve 给单点初值导致跳根**：定弦长方程在错误初值下可能收敛到反向/远处的根。用 `brentq` + 单调区间（沿运动方向）更稳，务必检查 $g(lo)g(hi)<0$。
4. **角度单位**：全程用弧度；`cos/sin` 以弧度为输入。
5. **盘入方向符号**：盘入是半径减小，需明确 $\theta$ 增大对应向心还是离心，并与题图（A 点位置、顺/逆时针）一致，否则全队坐标镜像出错。
6. **直线距离 vs 弧长距离混用**：铰接杆约束是**直线**距离（弦长）= 杆长，不是沿曲线弧长；两者只在小段近似相等。
7. **曲率公式忘了归一化**：单位法向量、曲率公式分母含 $|\mathbf r'|$ 的幂，漏掉会量纲错误。
8. **终止/相切点未用根查找精化**：盘入终止时刻、与圆弧相切点应由事件定位（根查找）求，而非粗网格扫描。
9. **大圈数累积误差**：逐点 `brentq` 链式递推，建议每点保留 $\geq10^{-10}$ 容差并核对相邻距离残差（模板已打印）。
10. **可视化缺轨迹图**：螺线类论文必须画出螺线 + 各关键点；只给坐标表扣分。

## 在建模比赛中的典型应用

- **CUMCM 2024A 板凳龙**：龙头沿等距螺线盘入，后方板凳铰接跟随。问题 1 = 在 $r=a+b\theta$ 上由龙头位置逐节定弦长反解所有把手坐标（本文模板的核心）；问题 2 用拿到的坐标做碰撞检测（见 [[collision_detection]]）；问题 3 对螺距做一维可行性搜索（见 [[root_finding]]）。
- 等距螺线 / 螺旋布局类：螺旋天线、卷绕、螺旋扫描覆盖。
- 一般平面参数曲线的弧长 / 切法向量 / 曲率分析（与 [[path_kinematics]] 配合做速度传递）。

## 参考文献

- 司守奎 & 孙兆亮 (2015). 《数学建模算法与应用》(第二版). 几何与运动学相关章节.
- do Carmo, M. P. (1976). *Differential Geometry of Curves and Surfaces*. Prentice-Hall.（曲率 / 弧长）
- SciPy: `scipy.integrate.quad`, `scipy.optimize.brentq` 官方文档. https://docs.scipy.org/doc/scipy/
