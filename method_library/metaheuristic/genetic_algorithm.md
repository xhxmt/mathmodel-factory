# 遗传算法 (Genetic Algorithm, GA)

## 适用场景

- 单目标、多变量**非线性 / 非凸 / 多峰**优化，目标可为黑箱（不可导也行）
- 连续、离散、或混合变量（本文给实数编码；排列 / 组合可改编码与算子）
- 梯度方法易陷局部最优、或目标不可微 / 不连续时的全局搜索
- 作为 NLP 的**全局兜底 / 多起点替代**，再用 `optimization/nonlinear_programming.md` 局部精修

不适用：
- 目标光滑且单峰 → 直接 `scipy.optimize.minimize`（更快更准）
- 全线性 + 整数 → `optimization/milp.md`（精确最优）
- 连续单峰全局 → PSO / differential_evolution 往往更省（见 [[pso]]）
- 高维且预算紧 → 启发式收敛慢，需控制评估次数

## 核心假设

1. 目标函数可在任意点求值（黑箱可），评估代价可接受
2. 解可编码为定长向量；约束以**罚函数**或修复算子并入适应度
3. 求**近似**全局最优即可（GA 不保证全局最优，需多次独立运行核对）
4. 适应度按"越大越好"定义（最小化问题取负）

## 数学形式 / 算法

种群 $P=\{\mathbf x_1,\dots,\mathbf x_N\}$，适应度 $\mathrm{fit}(\mathbf x)=\text{obj}(\mathbf x)-\text{penalty}(\mathbf x)$。每代：

1. **选择**（锦标赛）：随机取 $k$ 个比适应度，胜者进入交配池
2. **交叉**（SBX，模拟二进制交叉，概率 $p_c$）：由两亲本生成两子代
3. **变异**（多项式变异，概率 $p_m$）：对基因加扰动
4. **精英保留**：最优若干个体直接进入下一代
5. 重复直到收敛（连续 `patience` 代无改进）或达 `max_gen`

## 求解工具

- `numpy` — 自实现（本文模板，完全可控、可定制编码 / 算子）
- `scipy.optimize.differential_evolution` — 同类全局进化算法，开箱即用、少调参
- `pymoo` / `DEAP` — 成熟进化算法库（多目标 NSGA-II 等）
- MATLAB 对照：`ga`（Global Optimization Toolbox）

## 代码模板

```python
"""
genetic_algorithm_template.py — 实数编码 GA（锦标赛 + SBX + 多项式变异 + 精英）
用法: python genetic_algorithm_template.py   # 或 solver_submit.sh --type python
问题适配点: 改 ObjectiveFunction.bounds / objective / constraint_penalty。
"""
import json
import os
import numpy as np

np.random.seed(42)

class ObjectiveFunction:
    def __init__(self):
        self.bounds = [(-5.12, 5.12), (-5.12, 5.12)]   # TODO: 替换变量范围
        self.n_var = len(self.bounds)

    def objective(self, x):                            # GA 默认最大化；最小化取负
        # 示例：最小化 Rastrigin → 取负作为适应度目标
        A = 10.0
        return -(A * self.n_var + np.sum(x**2 - A * np.cos(2*np.pi*x)))

    def constraint_penalty(self, x):
        pen = 0.0
        # 示例约束 x0 + x1 <= 6：if x[0]+x[1] > 6: pen += 1e6*(x[0]+x[1]-6)**2
        return pen

    def fitness(self, x):
        return self.objective(x) - self.constraint_penalty(x)

class GA:
    def __init__(self, obj, pop_size=60, pc=0.8, pm=0.1, max_gen=300,
                 elite_rate=0.05, tol=1e-8, patience=60):
        self.obj, self.pop_size, self.pc, self.pm = obj, pop_size, pc, pm
        self.max_gen, self.tol, self.patience = max_gen, tol, patience
        self.elite = max(1, int(pop_size * elite_rate))
        self.nv, self.bounds = obj.n_var, obj.bounds

    def _init(self):
        pop = np.empty((self.pop_size, self.nv))
        for i, (lo, hi) in enumerate(self.bounds):
            pop[:, i] = np.random.uniform(lo, hi, self.pop_size)
        return pop

    def _tournament(self, pop, fit, k=3):
        out = np.empty_like(pop)
        for i in range(self.pop_size):
            c = np.random.choice(self.pop_size, k, replace=False)
            out[i] = pop[c[np.argmax(fit[c])]]
        return out

    def _sbx(self, p1, p2, eta=20):
        c1, c2 = p1.copy(), p2.copy()
        for i, (lo, hi) in enumerate(self.bounds):
            if np.random.random() < self.pc and abs(p1[i]-p2[i]) > 1e-12:
                u = np.random.random()
                beta = (2*u)**(1/(eta+1)) if u <= 0.5 else (1/(2*(1-u)))**(1/(eta+1))
                c1[i] = np.clip(0.5*((1+beta)*p1[i] + (1-beta)*p2[i]), lo, hi)
                c2[i] = np.clip(0.5*((1-beta)*p1[i] + (1+beta)*p2[i]), lo, hi)
        return c1, c2

    def _mutate(self, ind, eta=20):
        m = ind.copy()
        for i, (lo, hi) in enumerate(self.bounds):
            if np.random.random() < self.pm:
                m[i] = np.clip(m[i] + np.random.normal(0, (hi-lo)*0.1), lo, hi)
        return m

    def run(self, verbose=True):
        pop = self._init()
        fit = np.array([self.obj.fitness(x) for x in pop])
        best_x, best_f, no_imp = pop[fit.argmax()].copy(), fit.max(), 0
        hist = []
        for g in range(self.max_gen):
            elites = pop[np.argsort(fit)[-self.elite:]].copy()
            sel = self._tournament(pop, fit)
            off = np.empty_like(pop)
            for i in range(0, self.pop_size, 2):
                if i+1 < self.pop_size:
                    off[i], off[i+1] = self._sbx(sel[i], sel[i+1])
                else:
                    off[i] = sel[i]
            off = np.array([self._mutate(c) for c in off])
            off[:self.elite] = elites
            pop = off
            fit = np.array([self.obj.fitness(x) for x in pop])
            hist.append(float(fit.max()))
            if fit.max() > best_f + self.tol:
                best_f, best_x, no_imp = fit.max(), pop[fit.argmax()].copy(), 0
            else:
                no_imp += 1
            if verbose and g % 50 == 0:
                print(f"gen {g:4d} | best={best_f:.6f}")
            if no_imp >= self.patience:
                print(f"收敛于第 {g} 代"); break
        return best_x, best_f, hist

if __name__ == "__main__":
    obj = ObjectiveFunction()
    x, f, hist = GA(obj).run()
    print(f"最优解 x = {x}")
    print(f"最优适应度 = {f:.6f}  (Rastrigin 全局最优在原点, 目标值 0)")
    os.makedirs("results", exist_ok=True)
    with open("results/ga_demo.json", "w") as fh:
        json.dump({"x": x.tolist(), "fitness": float(f), "history_tail": hist[-5:]},
                  fh, ensure_ascii=False, indent=2)
    print("written results/ga_demo.json")
```

## 常见陷阱

1. **跑一次就下结论**：GA 是随机算法；必须**多次独立运行**（不同种子）并报告最优 / 中位 / 方差，证明结论不靠运气。
2. **不固定随机种子**：论文结果不可复现；模板已 `np.random.seed(42)`，报告时说明种子。
3. **约束只靠罚函数且罚因子乱设**：罚太小约束被违反、太大搜索受阻；分级加大、或用修复算子保证可行。
4. **早熟收敛**：多样性丢失后停在局部最优；提高变异率 / 种群规模 / 引入随机重启。
5. **最大化 / 最小化弄反**：本实现按适应度**最大化**；最小化目标记得取负。
6. **编码与算子不匹配问题**：排列 / 组合问题用实数 SBX 无意义，应改用顺序交叉 / 交换变异等专用算子。
7. **评估代价高却不缓存**：目标含昂贵仿真时应缓存 / 并行评估，否则代数 × 种群规模评估爆炸。
8. **参数照抄默认**：`pop_size/pc/pm/max_gen` 需按问题规模调；对小问题可先试 `differential_evolution` 省调参。
9. **不与精确 / 局部法对照**：能用 NLP / MILP 求（近似）最优的子问题，应给对照说明 GA 解可信（见 [[nonlinear_programming]]、[[milp]]）。
10. **收敛曲线缺失**：进化算法论文应给最优适应度随代数的收敛曲线。

## 在建模比赛中的典型应用

- 复杂非线性 / 非凸优化的全局搜索：选址 + 调度耦合、参数寻优、布局优化。
- 作为 MILP / NLP 难以表达的目标（黑箱仿真目标）的求解器。
- 与局部法配合：GA 找好的初始区域 → `nonlinear_programming.md` 精修。
- 真实 CUMCM：含复杂目标的优化题常以 GA / PSO / SA 作为主或对照求解器（与 [[pso]]、[[simulated_annealing]] 互为对照增强可信度）。

## 参考文献

- Goldberg, D. E. (1989). *Genetic Algorithms in Search, Optimization, and Machine Learning*. Addison-Wesley.
- Deb, K. (2001). *Multi-Objective Optimization Using Evolutionary Algorithms*. Wiley.（SBX / 多项式变异）
- 司守奎 & 孙兆亮 (2015). 《数学建模算法与应用》(第二版). 智能优化算法章节.
- SciPy `differential_evolution` 文档. https://docs.scipy.org/doc/scipy/
