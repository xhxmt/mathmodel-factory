# 方法库 (method_library)

本目录是 Modeling Factory 的"方法字典"。`step0_problem_parsing` agent 读这里来做候选方法预选；后续 step 写代码时也以这里的代码模板为出发点（再按题目改）。

`index.json` 是机器可检索的 HMML-lite 登记表；`scripts/method_retrieve.py` 会读取它并按题目文本输出候选短名单。README 仍是人类阅读入口，方法正文仍以各 `.md` 文件为准。

## 目录约定

```
method_library/
  README.md                ← 本文件，索引
  index.json               ← 结构化方法登记表（供 scripts/method_retrieve.py 使用）
  evaluation/              ← 评价/赋权类
    ahp.md
    topsis.md
  optimization/            ← 优化类
    milp.md
    nonlinear_programming.md
  prediction/              ← 预测类
    arima.md
  dynamics/                ← 动力学/微分方程类
    ode_system.md
    path_kinematics.md
  geometry/                ← 几何/运动学类
    archimedean_spiral.md
    collision_detection.md
  numerical/               ← 数值方法类
    root_finding.md
  metaheuristic/           ← 元启发式/智能优化类
    genetic_algorithm.md
    pso.md
    simulated_annealing.md
```

已实现：`metaheuristic/`（GA, PSO, SA）、`geometry/`（等距螺线、碰撞检测）、`numerical/`（求根/事件定位）、`dynamics/path_kinematics.md`、`optimization/nonlinear_programming.md`。

未来扩展（占位，先不实现）：`classification/`（logistic, svm, xgboost）、`network/`（最短路、最大流、复杂网络）。

## 索引表

| 文件 | 方法 | 一句话适用场景 |
| --- | --- | --- |
| [evaluation/ahp.md](evaluation/ahp.md) | 层次分析法 AHP | 多准则决策、主观赋权，准则数 ≤ 9 |
| [evaluation/topsis.md](evaluation/topsis.md) | 优劣解距离法 TOPSIS | 多方案排序，已知或可获得权重 |
| [optimization/milp.md](optimization/milp.md) | 混合整数线性规划 | 调度 / 选址 / 生产决策 / 0-1 选择 |
| [prediction/arima.md](prediction/arima.md) | ARIMA / SARIMA | 单变量时间序列预测，含季节性 |
| [dynamics/ode_system.md](dynamics/ode_system.md) | 常微分方程组 | 连续动力学过程（传染病 / 反应 / 力学 / 人口） |
| [dynamics/path_kinematics.md](dynamics/path_kinematics.md) | 路径运动学 / 链式速度传递 | 多点沿同一曲线、刚性杆连接，速度沿链传递 / 速度放大系数 |
| [geometry/archimedean_spiral.md](geometry/archimedean_spiral.md) | 阿基米德螺线 / 曲线运动学 | 等距螺线位置 / 弧长 / 切法向量 / 定弦长反解（铰接递推） |
| [geometry/collision_detection.md](geometry/collision_detection.md) | 碰撞 / 干涉检测 (SAT) | 有向矩形 / 凸多边形互不重叠约束，碰撞对 / 临界间距 |
| [numerical/root_finding.md](numerical/root_finding.md) | 方程求根 / 事件定位 | 解 f(x)=0、临界时刻 / 阈值 / 相切点、一维可行性搜索 |
| [optimization/nonlinear_programming.md](optimization/nonlinear_programming.md) | 非线性规划 NLP | 连续变量约束最优化（非线性目标 / 约束，SLSQP / 全局兜底） |
| [metaheuristic/genetic_algorithm.md](metaheuristic/genetic_algorithm.md) | 遗传算法 GA | 单目标非凸 / 多峰 / 黑箱 / 离散全局搜索 |
| [metaheuristic/pso.md](metaheuristic/pso.md) | 粒子群 PSO | 连续变量非凸 / 多峰全局优化，调参少收敛快 |
| [metaheuristic/simulated_annealing.md](metaheuristic/simulated_annealing.md) | 模拟退火 SA | 组合优化 / 粗糙多峰，单解迭代、可跳出局部最优 |

## 每个方法文档的结构（写新方法时按此模板）

1. **适用场景**：哪类问题用这个方法最自然，哪些边界情况要切换
2. **核心假设**：使用前提，违反时的后果
3. **数学形式**：标准记号下的最简表达
4. **求解工具**：推荐库、求解器、版本要求
5. **代码模板**：可直接 `solver_submit.sh --type python` 跑通的最小示例
6. **常见陷阱**：建模比赛中容易踩的坑（赋权方向、平稳性、刚性、对偶可行性等）
7. **在建模比赛中的典型应用**：CUMCM 历年题目里以这个方法作为主/辅工具的例子
8. **参考文献**：教材、综述、官方文档链接

## 可审计模板要求（给 Step 4/5 agent）

每个方法文档里的代码模板不只是演示算法，还必须能接入 Modeling Factory 的质量门禁。新建或更新方法时，模板至少说明并尽量示范以下输出：

- `results/pN/values.json` 或 `results/canonical_results.json`：包含 `status`、主目标值/关键结果、核心决策变量、随机种子、求解器状态和运行时间。禁止只把最终数值写进论文或临时日志。
- `results/pN/solver.log` 或 `logs/<method>.log`：保留求解器原始状态、gap/收敛信息、警告和异常。
- `assumption_ledger.md` 需要登记的核心假设、适用边界和违反后果。
- 至少一个 sanity check 或小规模反例：说明该方法在什么输入下应通过、什么输入下应拒绝或降级。
- 若方法生成 `result*.xlsx` 或论文表格，必须从同一 canonical 结果源派生，不能手工重填另一套数字。

## 使用规则（给 agent 看）

- step0 的 `candidate_methods.md` 必须用本目录的相对路径引用方法（如 `method_library/optimization/milp.md`），不能凭空写未登记的方法名。
- 若题目需要的方法不在本目录，新建条目并在 README 索引表登记。**绝不**在 prompt 阶段杜撰方法。
- 代码模板默认 Python（`numpy`/`scipy`/`statsmodels`/`gurobipy`），保持 `modeling_guide.md` 的可复现要求（固定随机种子、显式输出文件路径）。
- 一篇方法文档不超过 ~300 行；如果展开复杂变体，新建 `<name>_variant.md` 而不是把单文件变厚。
