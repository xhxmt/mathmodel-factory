# 方法库 (method_library)

本目录是 Modeling Factory 的"方法字典"。`step0_problem_parsing` agent 读这里来做候选方法预选；后续 step 写代码时也以这里的代码模板为出发点（再按题目改）。

## 目录约定

```
method_library/
  README.md                ← 本文件，索引
  evaluation/              ← 评价/赋权类
    ahp.md
    topsis.md
  optimization/            ← 优化类
    milp.md
  prediction/              ← 预测类
    arima.md
  dynamics/                ← 动力学/微分方程类
    ode_system.md
```

未来扩展（占位，先不实现）：`classification/`（logistic, svm, xgboost）、`network/`（最短路、最大流、复杂网络）、`metaheuristic/`（GA, PSO, SA）。

## 索引表

| 文件 | 方法 | 一句话适用场景 |
| --- | --- | --- |
| [evaluation/ahp.md](evaluation/ahp.md) | 层次分析法 AHP | 多准则决策、主观赋权，准则数 ≤ 9 |
| [evaluation/topsis.md](evaluation/topsis.md) | 优劣解距离法 TOPSIS | 多方案排序，已知或可获得权重 |
| [optimization/milp.md](optimization/milp.md) | 混合整数线性规划 | 调度 / 选址 / 生产决策 / 0-1 选择 |
| [prediction/arima.md](prediction/arima.md) | ARIMA / SARIMA | 单变量时间序列预测，含季节性 |
| [dynamics/ode_system.md](dynamics/ode_system.md) | 常微分方程组 | 连续动力学过程（传染病 / 反应 / 力学 / 人口） |

## 每个方法文档的结构（写新方法时按此模板）

1. **适用场景**：哪类问题用这个方法最自然，哪些边界情况要切换
2. **核心假设**：使用前提，违反时的后果
3. **数学形式**：标准记号下的最简表达
4. **求解工具**：推荐库、求解器、版本要求
5. **代码模板**：可直接 `solver_submit.sh --type python` 跑通的最小示例
6. **常见陷阱**：建模比赛中容易踩的坑（赋权方向、平稳性、刚性、对偶可行性等）
7. **在建模比赛中的典型应用**：CUMCM 历年题目里以这个方法作为主/辅工具的例子
8. **参考文献**：教材、综述、官方文档链接

## 使用规则（给 agent 看）

- step0 的 `candidate_methods.md` 必须用本目录的相对路径引用方法（如 `method_library/optimization/milp.md`），不能凭空写未登记的方法名。
- 若题目需要的方法不在本目录，新建条目并在 README 索引表登记。**绝不**在 prompt 阶段杜撰方法。
- 代码模板默认 Python（`numpy`/`scipy`/`statsmodels`/`gurobipy`），保持 `modeling_guide.md` 的可复现要求（固定随机种子、显式输出文件路径）。
- 一篇方法文档不超过 ~300 行；如果展开复杂变体，新建 `<name>_variant.md` 而不是把单文件变厚。
