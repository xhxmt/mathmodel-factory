# 模拟退火 (Simulated Annealing, SA)

## 适用场景

- **组合优化**（TSP / 排程 / 分组 / 选择）与单目标连续优化
- 解空间粗糙、多局部最优，需以一定概率"爬出"局部最优
- 目标黑箱、不可导；只需定义邻域 / 扰动算子
- 单解迭代、内存小，适合大解空间的近似求解

不适用：
- 目标光滑单峰 → `scipy.optimize.minimize` 更快
- 全线性 + 整数 → `optimization/milp.md`（精确）
- 需要并行群体多样性 → GA / PSO（见 [[genetic_algorithm]]、[[pso]]）

## 核心假设

1. 能定义当前解的**邻域扰动**（连续：高斯扰动；组合：交换 / 翻转 / 插入）
2. 目标可在任意解上求值
3. 求近似全局最优即可（随机算法，需多次运行 / 多初值核对）
4. 温度调度（初温、衰减、终温）与问题尺度匹配

## 数学形式 / 算法

从当前解 $x$ 生成邻域解 $x'$，能量差 $\Delta=f(x')-f(x)$（最小化视角）。

- 若 $\Delta<0$（更优）：接受。
- 否则以 **Metropolis 概率** $P=\exp(-\Delta/T)$ 接受较差解。

温度 $T$ 按几何衰减 $T\leftarrow\alpha T$（$0<\alpha<1$），每个温度内做 $L$ 次内循环。高温多探索（易接受劣解），低温趋贪心（收敛）。终止：$T<T_{\min}$ 或达最大迭代。

## 求解工具

- `numpy` + `math` — 自实现（本文模板，连续与组合均可改邻域）
- `scipy.optimize.dual_annealing` — 成熟的（广义）模拟退火，连续全局优化开箱即用
- MATLAB 对照：`simulannealbnd`（Global Optimization Toolbox）

## 代码模板

```python
"""
simulated_annealing_template.py — 标准 SA（Metropolis + 几何降温），连续示例
用法: python simulated_annealing_template.py   # 或 solver_submit.sh --type python
问题适配点: 改 bounds / objective；组合问题改 _neighbor 为交换/翻转等算子。
"""
import json
import math
import os
import numpy as np

np.random.seed(42)

class ObjectiveFunction:
    def __init__(self):
        self.bounds = [(-5.0, 5.0), (-5.0, 5.0)]       # TODO: 替换
        self.n_var = len(self.bounds)

    def objective(self, x):                            # 最小化目标（SA 这里按最小化处理）
        # 示例：Rastrigin（全局最小 0 在原点）
        A = 10.0
        return A*self.n_var + np.sum(x**2 - A*np.cos(2*np.pi*x))

class SA:
    def __init__(self, obj, T0=1000.0, alpha=0.95, L=120, T_min=1e-3, max_iter=4000):
        self.obj, self.T0, self.alpha = obj, T0, alpha
        self.L, self.T_min, self.max_iter = L, T_min, max_iter
        self.nv, self.bounds = obj.n_var, obj.bounds

    def _rand(self):
        return np.array([np.random.uniform(lo, hi) for lo, hi in self.bounds])

    def _neighbor(self, x, T):
        y = x.copy()
        for i, (lo, hi) in enumerate(self.bounds):
            scale = (hi - lo) * 0.1 * (T / self.T0 + 0.01)     # 扰动随温度收缩
            y[i] = np.clip(y[i] + np.random.normal(0, scale), lo, hi)
        return y

    def run(self, verbose=True):
        cur = self._rand(); cur_f = self.obj.objective(cur)
        best, best_f = cur.copy(), cur_f
        T, hist = self.T0, []
        for it in range(self.max_iter):
            for _ in range(self.L):
                cand = self._neighbor(cur, T)
                cand_f = self.obj.objective(cand)
                delta = cand_f - cur_f                  # 最小化：<0 更优
                if delta < 0 or np.random.random() < math.exp(-delta / max(T, 1e-12)):
                    cur, cur_f = cand, cand_f
                    if cur_f < best_f:
                        best, best_f = cur.copy(), cur_f
            T *= self.alpha
            hist.append(float(best_f))
            if verbose and it % 50 == 0:
                print(f"iter {it:5d} | T={T:8.3f} | best={best_f:.6f}")
            if T < self.T_min:
                print(f"降温至 T_min, 停于第 {it} 轮"); break
        return best, best_f, hist

if __name__ == "__main__":
    obj = ObjectiveFunction()
    x, f, hist = SA(obj).run()
    print(f"最优解 x = {x}")
    print(f"最优目标值 = {f:.6f}  (Rastrigin 全局最小 0 在原点)")
    os.makedirs("results", exist_ok=True)
    with open("results/sa_demo.json", "w") as fh:
        json.dump({"x": x.tolist(), "objective": float(f), "history_tail": hist[-5:]},
                  fh, ensure_ascii=False, indent=2)
    print("written results/sa_demo.json")
```

## 常见陷阱

1. **初温 / 降温率乱设**：初温过低则一开始就贪心（陷局部最优），过高则前期纯随机浪费；初温应使初始接受率约 0.8–0.95，$\alpha$ 取 0.9–0.99。
2. **内循环长度 $L$ 太短**：每个温度未充分搜索就降温，等于快速贪心；$L$ 随问题规模设。
3. **邻域算子不当**：连续用高斯扰动、组合用交换 / 翻转 / 2-opt；用错算子搜索效率极低。
4. **最大化 / 最小化混淆**：本模板按**最小化**实现（$\Delta<0$ 更优）；最大化目标取负或改判据。
5. **单次运行 / 不固定种子**：随机算法需多次运行 + 固定种子报告稳定性与可复现性。
6. **降温过快**：$\alpha$ 太小快速冷却，错过全局最优；适当放慢或用重升温（reheating）。
7. **接受概率数值溢出**：$\Delta/T$ 很大时 `exp` 下溢为 0（正常）；$T\to0$ 要保护除零（模板用 `max(T,1e-12)`）。
8. **约束处理缺失**：组合问题邻域要保证可行（修复 / 拒绝），连续问题用罚函数。
9. **缺收敛 / 温度曲线**：SA 论文应给最优值随迭代、温度随迭代的曲线。
10. **不与对照 / 精确法比较**：小规模可与精确解或 GA / PSO 对照，证明 SA 解可信（见 [[genetic_algorithm]]、[[pso]]）。

## 在建模比赛中的典型应用

- 组合优化：路径 / 排程 / 分组 / 选择类问题（TSP、车辆路径、任务指派的近似求解）。
- 粗糙多峰连续目标的全局优化（参数寻优、布局优化）。
- 与 GA / PSO 并列作对照实验；或为 NLP 提供全局起点（见 [[nonlinear_programming]]）。
- 真实 CUMCM：含 NP-难组合结构或多峰目标的优化题常用 SA 作主 / 辅求解器。

## 参考文献

- Kirkpatrick, S., Gelatt, C. D., & Vecchi, M. P. (1983). Optimization by Simulated Annealing. *Science*, 220(4598).
- Aarts, E. & Korst, J. (1989). *Simulated Annealing and Boltzmann Machines*. Wiley.
- 司守奎 & 孙兆亮 (2015). 《数学建模算法与应用》(第二版). 智能优化算法章节.
- SciPy `dual_annealing` 文档. https://docs.scipy.org/doc/scipy/
