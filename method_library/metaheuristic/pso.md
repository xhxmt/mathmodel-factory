# 粒子群优化 (Particle Swarm Optimization, PSO)

## 适用场景

- **连续**变量的非线性 / 非凸 / 多峰全局优化，目标可为黑箱
- 维度中等、目标评估不太昂贵时收敛快、实现简单、调参少
- 梯度法易陷局部最优时的全局搜索；NLP 的全局起点来源
- 与 GA / SA 互为对照，增强求解可信度

不适用：
- 离散 / 排列 / 组合变量（PSO 原生连续；离散化或改用 GA / SA）
- 目标光滑单峰 → 直接 `scipy.optimize.minimize` 更快更准
- 全线性 + 整数 → `optimization/milp.md`

## 核心假设

1. 决策变量连续、有界（每维给 `[lo, hi]`）
2. 目标可在任意点求值；约束以罚函数并入适应度
3. 求近似全局最优即可（随机算法，需多次运行核对）
4. 适应度"越大越好"（最小化取负）

## 数学形式 / 算法

每个粒子有位置 $\mathbf x_i$ 与速度 $\mathbf v_i$，记个体历史最优 $\mathbf p_i$、全局最优 $\mathbf g$。每次迭代：

$$\mathbf v_i\leftarrow w\,\mathbf v_i+c_1 r_1(\mathbf p_i-\mathbf x_i)+c_2 r_2(\mathbf g-\mathbf x_i),\qquad \mathbf x_i\leftarrow \mathbf x_i+\mathbf v_i$$

其中惯性权重 $w$ 线性递减（前期探索、后期收敛），$c_1,c_2$ 为认知 / 社会系数，$r_1,r_2\sim U(0,1)$。速度与位置都做边界 / 限速裁剪。

## 求解工具

- `numpy` — 自实现（本文模板）
- `pyswarms` — 成熟 PSO 库
- `scipy.optimize.differential_evolution` — 同类全局优化的稳健替代
- MATLAB 对照：`particleswarm`（Global Optimization Toolbox）

## 代码模板

```python
"""
pso_template.py — 惯性权重线性递减的标准 PSO（连续全局优化）
用法: python pso_template.py   # 或 solver_submit.sh --type python
问题适配点: 改 ObjectiveFunction.bounds / objective / constraint_penalty。
"""
import json
import os
import numpy as np

np.random.seed(42)

class ObjectiveFunction:
    def __init__(self):
        self.bounds = [(-5.0, 5.0), (-5.0, 5.0)]      # TODO: 替换变量范围
        self.n_var = len(self.bounds)

    def objective(self, x):                            # 默认最大化；最小化取负
        # 示例：最小化 (x0-3)^2 + (x1+1)^2 → 取负
        return -((x[0]-3.0)**2 + (x[1]+1.0)**2)

    def constraint_penalty(self, x):
        return 0.0                                     # TODO: 约束 → 罚项

    def fitness(self, x):
        return self.objective(x) - self.constraint_penalty(x)

class PSO:
    def __init__(self, obj, n=40, max_iter=300, w0=0.9, w1=0.4,
                 c1=2.0, c2=2.0, tol=1e-8, patience=60):
        self.obj, self.n, self.max_iter = obj, n, max_iter
        self.w0, self.w1, self.c1, self.c2 = w0, w1, c1, c2
        self.tol, self.patience = tol, patience
        self.nv, self.bounds = obj.n_var, obj.bounds

    def run(self, verbose=True):
        lo = np.array([b[0] for b in self.bounds])
        hi = np.array([b[1] for b in self.bounds])
        X = np.random.uniform(lo, hi, (self.n, self.nv))
        V = np.random.uniform(-(hi-lo)*0.1, (hi-lo)*0.1, (self.n, self.nv))
        fit = np.array([self.obj.fitness(x) for x in X])
        pbest, pbest_f = X.copy(), fit.copy()
        gi = fit.argmax(); g, gf = X[gi].copy(), fit[gi]
        hist, no_imp = [], 0
        vmax = (hi - lo) * 0.2
        for it in range(self.max_iter):
            w = self.w0 - (self.w0 - self.w1) * it / self.max_iter
            r1, r2 = np.random.random((self.n, self.nv)), np.random.random((self.n, self.nv))
            V = w*V + self.c1*r1*(pbest - X) + self.c2*r2*(g - X)
            V = np.clip(V, -vmax, vmax)
            X = np.clip(X + V, lo, hi)
            fit = np.array([self.obj.fitness(x) for x in X])
            imp = fit > pbest_f
            pbest[imp], pbest_f[imp] = X[imp], fit[imp]
            if fit.max() > gf + self.tol:
                gi = fit.argmax(); g, gf, no_imp = X[gi].copy(), fit.max(), 0
            else:
                no_imp += 1
            hist.append(float(gf))
            if verbose and it % 50 == 0:
                print(f"iter {it:4d} | best={gf:.6f}")
            if no_imp >= self.patience:
                print(f"收敛于第 {it} 次迭代"); break
        return g, gf, hist

if __name__ == "__main__":
    obj = ObjectiveFunction()
    x, f, hist = PSO(obj).run()
    print(f"最优解 x = {x}")
    print(f"最优适应度 = {f:.6f}  (示例全局最优在 (3,-1), 目标值 0)")
    os.makedirs("results", exist_ok=True)
    with open("results/pso_demo.json", "w") as fh:
        json.dump({"x": x.tolist(), "fitness": float(f), "history_tail": hist[-5:]},
                  fh, ensure_ascii=False, indent=2)
    print("written results/pso_demo.json")
```

## 常见陷阱

1. **单次运行下结论**：随机算法，需多种子多次运行核对稳定性。
2. **不固定种子**：不可复现；模板已设 `seed(42)`，论文注明。
3. **惯性权重恒定**：固定 $w$ 要么早熟要么不收敛；用线性 / 非线性递减平衡探索与开发。
4. **不限速**：速度无界会发散 / 越界乱跳；必须限速 + 位置裁剪。
5. **早熟（全收敛到一点）**：多样性丢失停在局部最优；增大种群 / 重启 / 加扰动。
6. **离散问题硬套**：PSO 原生连续；离散变量需取整 / 映射，常不如 GA / SA，对照选择。
7. **约束罚因子不当**：同 GA，太小违反、太大病态；分级或可行性保持。
8. **最大化 / 最小化弄反**：本实现最大化适应度，最小化取负。
9. **维度灾难**：高维收敛慢；控制评估预算或先降维。
10. **缺收敛曲线 / 对照**：应给全局最优随迭代曲线，并与 GA / SA / NLP 对照（见 [[genetic_algorithm]]、[[simulated_annealing]]、[[nonlinear_programming]]）。

## 在建模比赛中的典型应用

- 连续参数全局寻优：物理参数标定、布局 / 几何连续优化、控制参数整定。
- 作为 NLP 的全局起点：PSO 粗找区域 → `nonlinear_programming.md` 精修。
- 与 GA / SA 并列做对照实验，增强结论可信度（评委看重多方法验证）。

## 参考文献

- Kennedy, J. & Eberhart, R. (1995). Particle Swarm Optimization. *Proc. IEEE ICNN*.
- Shi, Y. & Eberhart, R. (1998). A Modified Particle Swarm Optimizer.（惯性权重）
- 司守奎 & 孙兆亮 (2015). 《数学建模算法与应用》(第二版). 智能优化算法章节.
